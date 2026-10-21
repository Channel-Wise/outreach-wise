import pytest
from conftest import make_campaign

from outreach_wise import campaigns, db, inbox, sender, sheets
from outreach_wise.rendering import MissingVariable, render, required_variables


def msg(sender="Jane <jane@leads.io>", subject="Re: Hi", snippet="Sounds good", **headers):
    return {
        "id": "x",
        "thread_id": "t",
        "snippet": snippet,
        "internal_date": 0,
        "labels": [],
        "headers": {"from": sender, "subject": subject, **headers},
    }


# --- classification ---------------------------------------------------------------------------


def test_classify_reply_bounce_auto_reply_unsubscribe():
    assert inbox.classify(msg()) == "reply"
    assert inbox.classify(msg(sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>")) == "bounce"
    assert inbox.classify(msg(subject="Undeliverable: Hi")) == "bounce"
    assert inbox.classify(msg(subject="Automatic reply: Hi")) == "auto_reply"
    assert inbox.classify(msg(**{"auto-submitted": "auto-replied"})) == "auto_reply"
    assert inbox.classify(msg(snippet="Please remove me from your list")) == "unsubscribe"
    assert inbox.classify(msg(subject="unsubscribe", snippet="")) == "unsubscribe"


def test_our_own_footer_in_quoted_text_is_not_an_unsubscribe():
    snippet = 'Interested, let&#39;s talk! On Wed, Oct 21, 2026 at 10:00 Me &lt;me@x.io&gt; wrote: reply "unsubscribe"'
    assert inbox.classify(msg(snippet=snippet)) == "reply"


# --- polling ----------------------------------------------------------------------------------


def test_poll_matches_threads_and_ignores_unrelated_mail(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=2)
    conn.execute("UPDATE accounts SET daily_limit = 10")
    sender.run_send_cycle(conn, cfg, google)
    clock.advance(hours=1)
    conn.execute("UPDATE accounts SET next_send_at = NULL")
    sender.run_send_cycle(conn, cfg, google)
    assert len(google.sent) == 2

    google.deliver(thread_id="t1", sender="colleague@leads.io", snippet="Forwarding to my boss", at=clock())
    google.deliver(
        thread_id="t2",
        sender="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification",
        snippet="Address not found",
        at=clock(),
    )
    google.deliver(
        thread_id="zzz", sender="newsletter@shop.com", subject="Big sale", snippet="private", at=clock()
    )
    account = conn.execute("SELECT * FROM accounts").fetchone()
    counts = inbox.poll(conn, cfg, google, account)
    assert counts == {"reply": 1, "bounce": 1, "ignored": 1}

    assert [r[0] for r in conn.execute("SELECT status FROM leads ORDER BY id")] == ["replied", "bounced"]
    assert db.is_suppressed(conn, "lead2@leads.io")
    ignored = conn.execute("SELECT * FROM inbound WHERE kind = 'ignored'").fetchone()
    assert ignored["subject"] is None and ignored["snippet"] is None and ignored["from_email"] is None

    # Polling again does not double count.
    assert inbox.poll(conn, cfg, google, conn.execute("SELECT * FROM accounts").fetchone()) == {}


def test_unsubscribe_link_flow_suppresses_lead(conn, cfg):
    make_campaign(conn, leads=1)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    inbox.apply_outcome(conn, lead["id"], "unsubscribe", db.now())
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "unsubscribed"
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "unsubscribed"
    assert db.is_suppressed(conn, lead["email"])


# --- sheet sync -------------------------------------------------------------------------------


def test_parse_rows_normalizes_and_deduplicates():
    rows = [
        ["E-mail", "First Name", "Channel URL"],
        ["Jane@Leads.io ", "Jane", "https://youtube.com/@jane"],
        ["jane@leads.io", "Duplicate", ""],
        ["not-an-email", "Bob", ""],
        ["", "", ""],
        ["sam@leads.io", "Sam"],  # short row
    ]
    leads, stats = sheets.parse_rows(rows, "e-mail")
    assert [lead["email"] for lead in leads] == ["jane@leads.io", "sam@leads.io"]
    assert leads[0]["fields"] == {
        "e_mail": "Jane@Leads.io",
        "first_name": "Jane",
        "channel_url": "https://youtube.com/@jane",
    }
    assert stats == {"rows": 4, "invalid": 1, "duplicates": 1}


def test_parse_rows_requires_an_email_column():
    with pytest.raises(sheets.SyncError):
        sheets.parse_rows([["Name"], ["Jane"]])


def test_sync_upserts_and_keeps_status(conn, cfg, google, account):
    db.save_settings(conn, {"sheet_url": "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0"})
    google.sheet = [["Email", "Name"], ["a@leads.io", "A"], ["b@leads.io", "B"]]
    assert sheets.sync(conn, cfg, google)["created"] == 2
    conn.execute("UPDATE leads SET status = 'replied' WHERE email = 'a@leads.io'")
    google.sheet = [["Email", "Name"], ["a@leads.io", "A2"], ["b@leads.io", "B"], ["c@leads.io", "C"]]
    stats = sheets.sync(conn, cfg, google)
    assert (stats["created"], stats["updated"]) == (1, 1)
    row = conn.execute("SELECT status, fields FROM leads WHERE email = 'a@leads.io'").fetchone()
    assert row["status"] == "replied" and '"A2"' in row["fields"]


def test_sync_auto_enrolls_into_active_campaigns(conn, cfg, google, account):
    campaign_id = make_campaign(conn, leads=1)
    conn.execute("UPDATE campaigns SET auto_enroll = 1 WHERE id = ?", (campaign_id,))
    db.save_settings(conn, {"sheet_url": "abc123"})
    google.sheet = [["Email"], ["new@leads.io"]]
    assert sheets.sync(conn, cfg, google)["enrolled"] == 1


def test_spreadsheet_id_from_url():
    assert sheets.spreadsheet_id("https://docs.google.com/spreadsheets/d/1AbC-d_9/edit#gid=0") == "1AbC-d_9"
    assert sheets.spreadsheet_id("1AbC-d_9") == "1AbC-d_9"


# --- enrollment & templates ---------------------------------------------------------------------


def test_enroll_skips_suppressed_and_leads_in_other_active_campaigns(conn):
    first = make_campaign(conn, leads=3)
    conn.execute(
        "INSERT INTO leads (email, fields, created_at, updated_at) VALUES ('x@blocked.io', '{}', '', '')"
    )
    db.suppress(conn, "@blocked.io", "manual")
    second = campaigns.create(conn, "Second")
    assert campaigns.count_eligible(conn, second) == 0
    conn.execute("UPDATE enrollments SET status = 'completed' WHERE campaign_id = ?", (first,))
    assert campaigns.count_eligible(conn, second) == 3


def test_delete_step_renumbers(conn):
    campaign_id = make_campaign(conn, leads=0)
    campaigns.add_step(conn, campaign_id)
    steps = campaigns.steps_with_variants(conn, campaign_id)
    assert campaigns.delete_step(conn, steps[1]["id"]) is None
    assert [s["position"] for s in campaigns.steps_with_variants(conn, campaign_id)] == [1, 2]


def test_render_variables_and_fallbacks():
    assert render("Hi {{ first_name | there }}!", {}) == "Hi there!"
    assert render("Hi {{First_Name}}", {"first_name": "Jane"}) == "Hi Jane"
    assert render("{{x|}}ok", {}) == "ok"
    with pytest.raises(MissingVariable):
        render("Hi {{first_name}}", {"first_name": "  "})
    assert required_variables("{{a}} {{b|x}}") == {"a"}


def test_ab_significance():
    assert campaigns.p_value(30, 100, 10, 100) < 0.05
    assert campaigns.p_value(11, 100, 10, 100) > 0.5
