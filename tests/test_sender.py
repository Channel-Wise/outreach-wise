from datetime import timedelta

from conftest import SENDER, add_leads, make_campaign

from outreach_wise import campaigns, db, sender
from outreach_wise.google import AuthError, GoogleError, SendStateUnknown


def statuses(conn, table, column="status"):
    return [row[0] for row in conn.execute(f"SELECT {column} FROM {table} ORDER BY id")]


def test_dry_run_never_calls_gmail_and_keeps_leads_new(conn, cfg, google, account):
    make_campaign(conn, leads=3)
    assert sender.run_send_cycle(conn, cfg, google) == 3
    assert google.sent == []
    assert statuses(conn, "messages") == ["dry_run"] * 3
    assert statuses(conn, "leads") == ["new"] * 3
    assert {row["next_step"] for row in conn.execute("SELECT next_step FROM enrollments")} == {2}


def test_dry_run_works_without_any_inbox(conn, cfg, google):
    make_campaign(conn, leads=2)
    assert sender.run_send_cycle(conn, cfg, google) == 2


def test_nothing_is_sent_outside_the_window_or_when_paused(conn, cfg, google, account, clock):
    make_campaign(conn, live=True)
    clock.advance(hours=8)  # 18:00 UTC
    assert sender.run_send_cycle(conn, cfg, google) == 0
    clock.advance(hours=-8)
    db.save_settings(conn, {"sending_paused": True})
    assert sender.run_send_cycle(conn, cfg, google) == 0
    assert google.sent == []


def test_live_send_is_paced_and_respects_daily_limit(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=5)
    conn.execute("UPDATE accounts SET daily_limit = 3")
    conn.commit()
    assert sender.run_send_cycle(conn, cfg, google) == 1
    assert sender.run_send_cycle(conn, cfg, google) == 0  # paced: next send is minutes away
    for _ in range(20):
        clock.advance(minutes=30)
        sender.run_send_cycle(conn, cfg, google)
    assert len(google.sent) == 3  # daily limit
    assert statuses(conn, "leads").count("contacted") == 3


def test_live_email_content_and_headers(conn, cfg, google, account):
    db.save_settings(conn, {"from_name": "Orhun from Channel Wise"})
    make_campaign(conn, live=True, leads=1)
    sender.run_send_cycle(conn, cfg, google)
    mime = google.sent[0]["mime"]
    assert mime["To"] == "lead1@leads.io"
    assert mime["Subject"] == "Hi Name1"
    assert "Orhun from Channel Wise" in mime["From"] and SENDER in mime["From"]
    assert "mailto:me@sender.io?subject=unsubscribe" in mime["List-Unsubscribe"]
    assert mime.get_payload(decode=True).decode().startswith("Hello Name1!")


def test_follow_up_replies_in_the_same_thread(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=1, delay_days=1)
    sender.run_send_cycle(conn, cfg, google)
    clock.advance(hours=3)
    conn.execute("UPDATE accounts SET next_send_at = NULL")
    assert sender.run_send_cycle(conn, cfg, google) == 0  # follow-up not due yet
    clock.advance(hours=21)  # Thursday 10:00
    assert sender.run_send_cycle(conn, cfg, google) == 1
    first, follow_up = google.sent
    assert follow_up["thread_id"] == first["thread_id"]
    assert follow_up["mime"]["Subject"] == "Re: Hi Name1"
    assert follow_up["mime"]["In-Reply-To"] == "<g1@mail.gmail.com>"
    assert statuses(conn, "enrollments") == ["completed"]


def test_reply_stops_follow_up_just_before_sending(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=1, delay_days=1)
    sender.run_send_cycle(conn, cfg, google)
    google.deliver(thread_id="t1", sender="Name1 <lead1@leads.io>", at=clock.advance(hours=2))
    clock.advance(days=1)
    conn.execute("UPDATE accounts SET next_send_at = NULL")
    assert sender.run_send_cycle(conn, cfg, google) == 0
    assert len(google.sent) == 1
    assert statuses(conn, "enrollments") == ["replied"]
    assert statuses(conn, "leads") == ["replied"]
    assert conn.execute("SELECT replied_at FROM messages").fetchone()[0] is not None


def test_same_step_is_never_sent_twice(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=1)
    settings = db.get_settings(conn)
    stale = conn.execute("SELECT * FROM enrollments").fetchone()  # snapshot before the first send
    assert sender.process(conn, cfg, google, stale, account, clock(), settings) == "sent"
    assert sender.process(conn, cfg, google, stale, account, clock(), settings) == "skipped"
    assert len(google.sent) == 1


def test_unconfirmed_send_is_never_retried(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=1)
    google.fail_next = SendStateUnknown("read timeout")
    assert sender.run_send_cycle(conn, cfg, google) == 0
    assert statuses(conn, "messages") == ["unknown"]
    assert statuses(conn, "enrollments") == ["stopped"]
    for _ in range(5):
        clock.advance(hours=1)
        sender.run_send_cycle(conn, cfg, google)
    assert google.sent == []


def test_retryable_error_retries_later_with_the_same_message_row(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=1)
    google.fail_next = GoogleError("backend error", 503, retryable=True)
    sender.run_send_cycle(conn, cfg, google)
    assert statuses(conn, "messages") == ["failed"]
    clock.advance(minutes=10)
    assert sender.run_send_cycle(conn, cfg, google) == 0  # retry delay not over
    clock.advance(minutes=25)
    assert sender.run_send_cycle(conn, cfg, google) == 1
    assert [tuple(r) for r in conn.execute("SELECT status, attempts FROM messages")] == [("sent", 2)]


def test_auth_error_pauses_the_inbox(conn, cfg, google, account):
    make_campaign(conn, live=True, leads=1)
    google.fail_next = AuthError("invalid_grant")
    sender.run_send_cycle(conn, cfg, google)
    row = conn.execute("SELECT enabled, last_error FROM accounts").fetchone()
    assert row["enabled"] == 0 and "invalid_grant" in row["last_error"]
    assert statuses(conn, "enrollments") == ["active"]  # will go out once the inbox is reconnected


def test_interrupted_send_is_marked_unknown_on_recovery(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=1)
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    step = conn.execute("SELECT * FROM steps WHERE position = 1").fetchone()
    variant = conn.execute("SELECT * FROM variants WHERE step_id = ?", (step["id"],)).fetchone()
    conn.execute(
        "INSERT INTO messages (enrollment_id, campaign_id, lead_id, step_id, variant_id, account_id, mode, status,"
        " to_email, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?, 'live', 'sending', 'x', 's', 'b', ?)",
        (
            enrollment["id"],
            enrollment["campaign_id"],
            enrollment["lead_id"],
            step["id"],
            variant["id"],
            account["id"],
            db.iso(clock()),
        ),
    )
    conn.commit()
    clock.advance(minutes=10)
    assert sender.recover_interrupted_sends(conn) == 1
    assert statuses(conn, "messages") == ["unknown"]
    assert statuses(conn, "enrollments") == ["stopped"]
    sender.run_send_cycle(conn, cfg, google)
    assert google.sent == []


def test_suppressed_reserved_and_broken_leads_are_skipped(conn, cfg, google, account):
    add_leads(conn, 1, domain="example.com")
    campaign_id = make_campaign(conn, live=False, leads=2)
    db.suppress(conn, "@leads.io", "manual")  # suppresses lead1 and lead2 after enrollment
    campaigns.go_live(conn, campaign_id)
    conn.commit()
    sender.run_send_cycle(conn, cfg, google)
    assert google.sent == []
    reasons = {row[0] for row in conn.execute("SELECT stop_reason FROM enrollments")}
    assert reasons == {"suppressed", "reserved test domain, never sent"}


def test_missing_variable_stops_lead_instead_of_sending(conn, cfg, google, account):
    conn.execute(
        "INSERT INTO leads (email, fields, created_at, updated_at) VALUES ('nofirst@leads.io', '{}', '', '')"
    )
    make_campaign(conn, live=True, leads=1)
    sender.run_send_cycle(conn, cfg, google)
    assert len(google.sent) == 1
    stopped = conn.execute("SELECT stop_reason FROM enrollments WHERE status = 'stopped'").fetchone()
    assert "first_name" in stopped[0]


def test_ab_variants_are_split_evenly(conn, cfg, google):
    campaign_id = make_campaign(conn, leads=10)
    step = campaigns.steps_with_variants(conn, campaign_id)[0]
    campaigns.add_variant(conn, step["id"])
    conn.commit()
    sender.run_send_cycle(conn, cfg, google)
    counts = dict(
        conn.execute(
            "SELECT v.label, COUNT(*) FROM messages m JOIN variants v ON v.id = m.variant_id GROUP BY v.label"
        ).fetchall()
    )
    assert counts == {"A": 5, "B": 5}


def test_go_live_resets_dry_run_progress(conn, cfg, google, account):
    campaign_id = make_campaign(conn, leads=2)
    sender.run_send_cycle(conn, cfg, google)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    campaigns.go_live(conn, campaign_id)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert {row[0] for row in conn.execute("SELECT next_step FROM enrollments")} == {1}
    conn.execute("UPDATE accounts SET daily_limit = 10")
    sender.run_send_cycle(conn, cfg, google)
    assert len(google.sent) == 1


def test_pacing_spreads_quota_over_the_window(clock):
    settings = db.DEFAULT_SETTINGS
    gap = sender.pacing_gap(clock(), settings, remaining=6)  # 7h left, 6 more emails after this one
    assert timedelta(minutes=40) <= gap <= timedelta(minutes=80)
    assert sender.pacing_gap(clock(), settings, remaining=1) < timedelta(hours=5)  # last one fits the window
