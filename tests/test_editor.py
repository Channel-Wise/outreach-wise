"""Lead picker, canvas JSON endpoints, per-variant follow-ups and the Results page."""

import pytest
from conftest import add_leads
from test_web import login

from outreach_wise import campaigns
from outreach_wise.web import create_app


@pytest.fixture
def client(cfg, conn, google):
    return create_app(cfg, google).test_client()


FETCH = {"X-Requested-With": "fetch"}


def enrolled_ids(conn, campaign_id):
    return {
        r[0] for r in conn.execute("SELECT lead_id FROM enrollments WHERE campaign_id = ?", (campaign_id,))
    }


def test_enroll_hand_picked_leads_only(conn):
    add_leads(conn, 6)
    campaign_id = campaigns.create(conn, "Pick")
    other = campaigns.create(conn, "Other")
    campaigns.enroll(conn, other, lead_ids=[5])  # lead 5 is busy in another active campaign
    assert campaigns.enroll(conn, campaign_id, lead_ids=[2, 4, 5, 999]) == 2
    assert enrolled_ids(conn, campaign_id) == {2, 4}


def test_enroll_random_sample_respects_limit(conn):
    add_leads(conn, 40)
    campaign_id = campaigns.create(conn, "Random")
    assert campaigns.enroll(conn, campaign_id, 10, shuffle=True) == 10
    assert enrolled_ids(conn, campaign_id) != set(range(1, 11))  # 1 in 847M chance of a false failure
    assert len(campaigns.eligible_leads(conn, campaign_id)) == 30


def test_enroll_route_modes(client, conn):
    add_leads(conn, 8)
    campaign_id = campaigns.create(conn, "Routes")
    conn.commit()
    token = login(client)
    client.post(f"/campaigns/{campaign_id}/enroll", data={"csrf": token, "how": "pick", "lead_ids": "3,7"})
    assert enrolled_ids(conn, campaign_id) == {3, 7}
    client.post(f"/campaigns/{campaign_id}/enroll", data={"csrf": token, "how": "first", "limit": "2"})
    assert enrolled_ids(conn, campaign_id) == {1, 2, 3, 7}
    client.post(f"/campaigns/{campaign_id}/enroll", data={"csrf": token, "how": "random", "limit": "3"})
    assert len(enrolled_ids(conn, campaign_id)) == 7


def test_sequence_edits_return_json(client, conn):
    campaign_id = campaigns.create(conn, "Canvas")
    conn.commit()
    token = login(client)
    data = client.get(f"/campaigns/{campaign_id}/sequence").get_json()
    assert [s["position"] for s in data["steps"]] == [1, 2]
    step = data["steps"][0]

    res = client.post(f"/steps/{step['id']}/variants", data={"csrf": token}, headers=FETCH)
    assert res.status_code == 200 and [v["label"] for v in res.get_json()["steps"][0]["variants"]] == [
        "A",
        "B",
    ]

    res = client.post(f"/campaigns/{campaign_id}/steps", data={"csrf": token}, headers=FETCH)
    assert len(res.get_json()["steps"]) == 3

    variant_id = step["variants"][0]["id"]
    res = client.post(
        f"/variants/{variant_id}",
        data={"csrf": token, "subject": "", "body": "x", "active": "on"},
        headers=FETCH,
    )
    assert res.status_code == 400 and res.get_json()["error"] == "The first email needs a subject."

    res = client.post(
        f"/variants/{variant_id}",
        data={"csrf": token, "subject": "Hey", "body": "x", "active": "on"},
        headers=FETCH,
    )
    assert (
        res.get_json()["ok"]
        and conn.execute("SELECT subject FROM variants WHERE id = ?", (variant_id,)).fetchone()[0] == "Hey"
    )


def _branch_campaign(conn, *, cover_b=True):
    """Step 1 has variants A and B; step 2 has a follow-up written for A (and for B if cover_b)."""
    from conftest import make_campaign

    campaign_id = make_campaign(conn, leads=4, delay_days=1)
    step1, step2 = campaigns.steps_with_variants(conn, campaign_id)
    campaigns.add_variant(conn, step1["id"])
    a, b = campaigns.steps_with_variants(conn, campaign_id)[0]["variants"]
    general = step2["variants"][0]
    assert campaigns.update_variant(conn, general["id"], "", "for A", True) is None
    assert campaigns.link(conn, general["id"], a["id"]) is None
    if cover_b:
        campaigns.add_variant(conn, step2["id"], parent_id=b["id"])
        b_follow = campaigns.steps_with_variants(conn, campaign_id)[1]["variants"][1]
        campaigns.update_variant(conn, b_follow["id"], "", "for B", True)
    conn.commit()
    return campaign_id, a, b


def test_each_variant_gets_its_own_follow_up(conn, cfg, google, clock):
    from outreach_wise import sender

    campaign_id, a, b = _branch_campaign(conn)
    assert sender.run_send_cycle(conn, cfg, google) == 4
    clock.advance(days=1)
    assert sender.run_send_cycle(conn, cfg, google) == 4
    rows = conn.execute(
        "SELECT m1.variant_id AS first, m2.body FROM messages m1 JOIN steps s1 ON s1.id = m1.step_id AND s1.position = 1"
        " JOIN messages m2 ON m2.enrollment_id = m1.enrollment_id AND m2.id != m1.id"
    ).fetchall()
    assert len(rows) == 4
    for row in rows:
        assert row["body"].startswith("for A" if row["first"] == a["id"] else "for B")


def test_leads_without_a_follow_up_for_their_variant_finish(conn, cfg, google, clock):
    from outreach_wise import sender

    campaign_id, a, b = _branch_campaign(conn, cover_b=False)
    step2 = campaigns.sequence(conn, campaigns.get(conn, campaign_id))[1]
    assert step2["uncovered"] == ["B"]
    sender.run_send_cycle(conn, cfg, google)
    clock.advance(days=1)
    sender.run_send_cycle(conn, cfg, google)
    reasons = [r[0] for r in conn.execute("SELECT stop_reason FROM enrollments ORDER BY id")]
    assert reasons.count("no step 2 email for variant B") == 2
    assert conn.execute("SELECT COUNT(*) FROM enrollments WHERE status = 'completed'").fetchone()[0] == 4


def test_branch_must_point_at_previous_step(conn):
    campaign_id = campaigns.create(conn, "Bad branch")
    step1, step2 = campaigns.steps_with_variants(conn, campaign_id)
    own = step2["variants"][0]["id"]
    assert campaigns.add_variant(conn, step2["id"], parent_id=own) is not None
    assert campaigns.link(conn, own, own) is not None
    assert campaigns.link(conn, step1["variants"][0]["id"], own) is not None  # backwards


def test_one_follow_up_can_serve_several_variants(conn, cfg, google, clock):
    """Step 1 has A, B, C. Follow-up X is linked to A and B; C gets the general follow-up."""
    from outreach_wise import sender

    campaign_id, a, b = _branch_campaign(conn, cover_b=False)
    step1, step2 = campaigns.steps_with_variants(conn, campaign_id)
    campaigns.add_variant(conn, step1["id"])
    c = campaigns.steps_with_variants(conn, campaign_id)[0]["variants"][2]
    x = step2["variants"][0]  # "for A", linked to A
    assert campaigns.link(conn, x["id"], b["id"]) is None
    campaigns.add_variant(conn, step2["id"])  # general follow-up, copied from the first variant
    general = campaigns.steps_with_variants(conn, campaign_id)[1]["variants"][1]
    campaigns.update_variant(conn, general["id"], "", "for C", True)
    conn.commit()

    seq = campaigns.sequence(conn, campaigns.get(conn, campaign_id))
    assert seq[1]["variants"][0]["parent_labels"] == ["A", "B"] and seq[1]["uncovered"] == []
    assert not any(v["unreached"] for v in seq[1]["variants"])

    sender.run_send_cycle(conn, cfg, google)
    clock.advance(days=1)
    sender.run_send_cycle(conn, cfg, google)
    for first, body in conn.execute(
        "SELECT m1.variant_id, m2.body FROM messages m1 JOIN steps s ON s.id = m1.step_id AND s.position = 1"
        " JOIN messages m2 ON m2.enrollment_id = m1.enrollment_id AND m2.id != m1.id"
    ):
        assert body.startswith("for C" if first == c["id"] else "for A")


def test_variant_linked_to_two_follow_ups_splits_its_leads(conn, cfg, google, clock):
    from conftest import make_campaign

    from outreach_wise import sender

    campaign_id = make_campaign(conn, leads=4, delay_days=1)
    step1, step2 = campaigns.steps_with_variants(conn, campaign_id)
    a = step1["variants"][0]
    campaigns.link(conn, step2["variants"][0]["id"], a["id"])
    campaigns.add_variant(conn, step2["id"], parent_id=a["id"])
    conn.commit()
    tests = campaigns.ab_results(conn, campaigns.get(conn, campaign_id))
    assert [len(t["variants"]) for t in tests if t["position"] == 2] == [2]  # an A/B test of follow-ups
    sender.run_send_cycle(conn, cfg, google)
    clock.advance(days=1)
    sender.run_send_cycle(conn, cfg, google)
    split = [
        n
        for (n,) in conn.execute(
            "SELECT COUNT(*) FROM messages WHERE step_id = ? GROUP BY variant_id", (step2["id"],)
        )
    ]
    assert split == [2, 2]


def test_unreached_general_follow_up_is_flagged(conn):
    campaign_id = campaigns.create(conn, "Orphan")
    step1, step2 = campaigns.steps_with_variants(conn, campaign_id)
    campaigns.add_variant(conn, step2["id"], parent_id=step1["variants"][0]["id"])
    conn.commit()
    variants = campaigns.sequence(conn, campaigns.get(conn, campaign_id))[1]["variants"]
    assert [v["unreached"] for v in variants] == [True, False]  # the general one gets nobody


def test_remove_leads_from_campaign(client, conn, cfg, google, account):
    from conftest import make_campaign

    from outreach_wise import sender

    campaign_id = make_campaign(conn, live=True, leads=3)
    sender.run_send_cycle(conn, cfg, google)  # lead 1 gets a real email (paced: one per cycle)
    token = login(client)
    client.post(f"/campaigns/{campaign_id}/unenroll", data={"csrf": token, "lead_id": ["1", "2"]})
    rows = dict(conn.execute("SELECT lead_id, status FROM enrollments").fetchall())
    assert rows == {1: "stopped", 3: "active"}  # lead 1 keeps its history, lead 2 is gone
    assert campaigns.enroll(conn, campaign_id, lead_ids=[2]) == 1  # and can be added back


def test_results_dashboard_renders(client, conn, cfg, google):
    from conftest import make_campaign

    from outreach_wise import sender

    campaign_id = make_campaign(conn, leads=3)
    sender.run_send_cycle(conn, cfg, google)
    login(client)
    page = client.get(f"/campaigns/{campaign_id}").data
    assert b"Sequence funnel" in page and b"Where leads are now" in page and b"Daily activity" in page


def test_connect_and_disconnect_on_canvas(client, conn):
    campaign_id = campaigns.create(conn, "Drag")
    step1, step2 = campaigns.steps_with_variants(conn, campaign_id)
    campaigns.add_variant(conn, step1["id"])
    conn.commit()
    a, b = campaigns.steps_with_variants(conn, campaign_id)[0]["variants"]
    follow = step2["variants"][0]["id"]
    token = login(client)

    def post(parent, action="connect"):
        return client.post(
            f"/variants/{follow}/links",
            data={"csrf": token, "parent_id": parent, "action": action},
            headers=FETCH,
        )

    data = post(b["id"]).get_json()
    assert data["ok"] and data["steps"][1]["variants"][0]["parent_labels"] == ["B"]
    assert data["steps"][1]["uncovered"] == ["A"]  # nobody gets a follow-up after A yet

    data = post(a["id"]).get_json()  # a second parent for the same follow-up
    assert (
        data["steps"][1]["variants"][0]["parent_labels"] == ["A", "B"] and data["steps"][1]["uncovered"] == []
    )

    data = post(b["id"], "disconnect").get_json()
    assert data["steps"][1]["variants"][0]["parent_labels"] == ["A"]

    assert post(follow).status_code == 400
