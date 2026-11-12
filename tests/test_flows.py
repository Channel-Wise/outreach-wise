"""End-to-end flow simulations: many leads through multi-step sequences with A/B splits,
per-variant follow-ups, custom percentages, replies, deletions — then check every lead took the
right path and every number the UI shows matches what actually happened.

Each email body starts with a unique tag ("S1-A", "S2-X", ...) so a lead's path can be read back
from the messages table and compared with the routing rules.
"""

from collections import Counter, defaultdict

import pytest
from conftest import add_leads

from outreach_wise import campaigns, db, sender


@pytest.fixture(autouse=True)
def roomy_settings(conn):
    # Dry runs follow the daily limit and sending days; lift both so a run is limited by the flow only.
    db.save_settings(conn, {"daily_limit": 100_000, "send_days": [0, 1, 2, 3, 4, 5, 6]})
    conn.commit()


# --- building flows ------------------------------------------------------------------------


class Flow:
    """A campaign built step by step: flow.variant(1, "A") etc. Bodies are tagged with S<step>-<tag>."""

    def __init__(self, conn, leads: int, steps: int = 2, delay: int = 1):
        self.conn = conn
        add_leads(conn, leads)
        self.id = campaigns.create(conn, "Flow")
        while len(self.steps()) < steps:
            campaigns.add_step(conn, self.id, delay_days=delay)
        for s in self.steps()[1:]:
            conn.execute("UPDATE steps SET delay_days = ? WHERE id = ?", (delay, s["id"]))
        self.ids: dict[tuple[int, str], int] = {}  # (step position, tag) -> variant id
        for s in self.steps():
            v = s["variants"][0]
            self._tag(s["position"], v["id"], "A")

    def steps(self):
        return campaigns.steps_with_variants(self.conn, self.id)

    def step(self, position):
        return self.steps()[position - 1]

    def _tag(self, position, variant_id, tag):
        subject = "Hello {{first_name}}" if position == 1 else ""
        assert campaigns.update_variant(self.conn, variant_id, subject, f"S{position}-{tag}", True) is None
        self.ids[(position, tag)] = variant_id

    def variant(self, position, tag, after: list[str] | None = None) -> int:
        """Add a variant to a step, optionally connected to previous-step variants (by tag)."""
        step = self.step(position)
        before = {v["id"] for v in step["variants"]}
        parents = [self.ids[(position - 1, t)] for t in after or []]
        assert campaigns.add_variant(self.conn, step["id"], parents[0] if parents else None) is None
        new = next(v for v in self.step(position)["variants"] if v["id"] not in before)
        self._tag(position, new["id"], tag)
        for parent in parents[1:]:
            assert campaigns.link(self.conn, new["id"], parent) is None
        return new["id"]

    def link(self, position, tag, after_tag, connect=True):
        err = campaigns.link(
            self.conn, self.ids[(position, tag)], self.ids[(position - 1, after_tag)], connect
        )
        assert err is None, err

    def split(self, position, after_tag, **shares):
        parent = self.ids[(position - 1, after_tag)] if after_tag else None
        percents = {self.ids[(position, tag)]: pct for tag, pct in shares.items()}
        err = campaigns.set_split(self.conn, self.step(position)["id"], parent, percents)
        assert err is None, err

    def start(self, enroll: int | None = None):
        campaigns.enroll(self.conn, self.id, enroll)
        self.conn.execute("UPDATE campaigns SET status = 'active' WHERE id = ?", (self.id,))
        self.conn.commit()

    def run(self, cfg, google, clock, days: int):
        """Run the worker once per day for `days` days (dry run: no inbox connected)."""
        for _ in range(days):
            sender.run_send_cycle(self.conn, cfg, google)
            clock.advance(days=1)
        sender.run_send_cycle(self.conn, cfg, google)

    def paths(self) -> dict[int, str]:
        """lead id -> "A>X>..." (tags of the emails it got, in step order)."""
        out = defaultdict(list)
        for lead_id, body in self.conn.execute(
            "SELECT m.lead_id, m.body FROM messages m JOIN steps s ON s.id = m.step_id"
            " WHERE m.campaign_id = ? ORDER BY m.lead_id, s.position",
            (self.id,),
        ):
            out[lead_id].append(body.split()[0].split("-")[1])
        return {lead: ">".join(tags) for lead, tags in out.items()}

    def enrollments(self) -> dict[int, tuple[str, str | None]]:
        return {
            r["lead_id"]: (r["status"], r["stop_reason"])
            for r in self.conn.execute("SELECT * FROM enrollments WHERE campaign_id = ?", (self.id,))
        }


def counts(paths: dict[int, str], depth: int) -> Counter:
    return Counter(">".join(p.split(">")[:depth]) for p in paths.values() if len(p.split(">")) >= depth)


# --- 1. first-email splits -----------------------------------------------------------------


def test_even_three_way_split(conn, cfg, google, clock):
    f = Flow(conn, leads=90, steps=1)
    f.variant(1, "B")
    f.variant(1, "C")
    f.start()
    f.run(cfg, google, clock, days=0)
    assert counts(f.paths(), 1) == {"A": 30, "B": 30, "C": 30}


def test_custom_first_email_split(conn, cfg, google, clock):
    f = Flow(conn, leads=100, steps=1)
    f.variant(1, "B")
    f.split(1, None, A=70, B=30)
    f.start()
    f.run(cfg, google, clock, days=0)
    assert counts(f.paths(), 1) == {"A": 70, "B": 30}


def test_uneven_split_stays_proportional_at_every_point(conn, cfg, google, clock):
    """Leads arrive over time (enrolled in batches); the split must hold within ±1 at every moment."""
    f = Flow(conn, leads=60, steps=1)
    f.variant(1, "B")
    f.variant(1, "C")
    f.split(1, None, A=50, B=30, C=20)
    f.conn.execute("UPDATE campaigns SET status = 'active' WHERE id = ?", (f.id,))
    for batch in range(6):
        campaigns.enroll(conn, f.id, 10)
        conn.commit()
        sender.run_send_cycle(conn, cfg, google)
        sent = 10 * (batch + 1)
        got = counts(f.paths(), 1)
        for tag, share in (("A", 0.5), ("B", 0.3), ("C", 0.2)):
            assert abs(got[tag] - sent * share) <= 1, (batch, got)
    assert counts(f.paths(), 1) == {"A": 30, "B": 18, "C": 12}


def test_zero_percent_variant_gets_nobody(conn, cfg, google, clock):
    f = Flow(conn, leads=20, steps=1)
    f.variant(1, "B")
    f.split(1, None, A=100, B=0)
    f.start()
    f.run(cfg, google, clock, days=0)
    assert counts(f.paths(), 1) == {"A": 20}


def test_inactive_variant_gets_nobody(conn, cfg, google, clock):
    f = Flow(conn, leads=20, steps=1)
    b = f.variant(1, "B")
    campaigns.update_variant(conn, b, "Hello {{first_name}}", "S1-B", False)
    f.start()
    f.run(cfg, google, clock, days=0)
    assert counts(f.paths(), 1) == {"A": 20}


# --- 2. routing follow-ups -----------------------------------------------------------------


def test_shared_follow_up_when_nothing_is_connected(conn, cfg, google, clock):
    f = Flow(conn, leads=40)
    f.variant(1, "B")
    f.start()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 20, "B>A": 20}
    assert {s for s, _ in f.enrollments().values()} == {"completed"}


def test_each_variant_gets_its_own_follow_up(conn, cfg, google, clock):
    f = Flow(conn, leads=40)
    f.variant(1, "B")
    f.link(2, "A", "A")  # follow-up A only after A
    f.variant(2, "X", after=["B"])
    f.start()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 20, "B>X": 20}


def test_one_follow_up_after_two_variants_and_everyone_else(conn, cfg, google, clock):
    f = Flow(conn, leads=60)
    f.variant(1, "B")
    f.variant(1, "C")
    f.link(2, "A", "A")
    f.link(2, "A", "B")  # follow-up A after A *and* B
    f.variant(2, "E")  # "everyone else" -> only C is left for it
    f.start()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 20, "B>A": 20, "C>E": 20}


def test_variant_split_across_two_follow_ups_with_custom_shares(conn, cfg, google, clock):
    f = Flow(conn, leads=100)
    f.variant(1, "B")
    f.link(2, "A", "A")
    f.variant(2, "Z", after=["A"])  # A's leads: A vs Z (new connection gets an equal share)
    f.variant(2, "Y", after=["B"])
    f.split(2, "A", A=80, Z=20)
    f.start()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 40, "A>Z": 10, "B>Y": 50}


def test_same_follow_up_can_have_different_shares_per_parent(conn, cfg, google, clock):
    """X is connected to A (30%) and to B (90%); the shares are kept per connection."""
    f = Flow(conn, leads=200)
    f.variant(1, "B")
    f.link(2, "A", "A")
    f.link(2, "A", "B")
    f.variant(2, "X", after=["A", "B"])
    f.split(2, "A", A=70, X=30)
    f.split(2, "B", A=10, X=90)
    f.start()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 70, "A>X": 30, "B>A": 10, "B>X": 90}


def test_leads_without_a_follow_up_finish_with_a_reason(conn, cfg, google, clock):
    f = Flow(conn, leads=40)
    f.variant(1, "B")
    f.link(2, "A", "A")  # nothing for B, and no "everyone else" email
    seq = campaigns.sequence(conn, campaigns.get(conn, f.id))
    assert seq[1]["uncovered"] == ["B"]
    f.start()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 1) == {"A": 20, "B": 20}
    assert counts(f.paths(), 2) == {"A>A": 20}
    ended = [e for e in f.enrollments().values() if e[1]]
    assert len(ended) == 20 and set(ended) == {("completed", "no step 2 email for variant B")}


def test_three_steps_route_on_the_previous_step_only(conn, cfg, google, clock):
    """Step 3 is chosen by the step-2 variant the lead got, not by the first email."""
    f = Flow(conn, leads=80, steps=3)
    f.variant(1, "B")
    f.variant(2, "X")  # step 2: A vs X for everyone (50/50)
    f.link(3, "A", "A")  # step 3: A after step-2 A
    f.variant(3, "Q", after=["X"])  # Q after step-2 X
    f.start()
    f.run(cfg, google, clock, days=2)
    paths = f.paths()
    assert len(paths) == 80 and all(len(p.split(">")) == 3 for p in paths.values())
    for p in paths.values():
        s2, s3 = p.split(">")[1:]
        assert s3 == ("A" if s2 == "A" else "Q"), p
    assert counts(paths, 1) == {"A": 40, "B": 40}
    assert counts(paths, 2) == {"A>A": 20, "A>X": 20, "B>A": 20, "B>X": 20}


def test_follow_ups_wait_the_configured_days(conn, cfg, google, clock):
    f = Flow(conn, leads=10, delay=3)
    f.start()
    f.run(cfg, google, clock, days=2)
    assert counts(f.paths(), 2) == {}  # not due yet after 2 days
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 10}


# --- 3. stopping ---------------------------------------------------------------------------


def test_replies_unsubscribes_and_removals_stop_follow_ups(conn, cfg, google, clock):
    f = Flow(conn, leads=12)
    f.start()
    f.run(cfg, google, clock, days=0)
    conn.execute("UPDATE leads SET status = 'replied' WHERE id IN (1, 2)")
    conn.execute("UPDATE leads SET status = 'unsubscribed' WHERE id = 3")
    db.suppress(conn, "lead4@leads.io", "manual")
    campaigns.unenroll(conn, f.id, [5])
    conn.commit()
    f.run(cfg, google, clock, days=1)
    paths = f.paths()
    assert 5 not in paths  # removed before any real email: gone entirely (dry-run history too)
    assert sorted(lead for lead, p in paths.items() if p == "A") == [1, 2, 3, 4]
    assert sorted(lead for lead, p in paths.items() if p == "A>A") == list(range(6, 13))
    stopped = {lead: reason for lead, (status, reason) in f.enrollments().items() if status == "stopped"}
    assert stopped == {1: "lead replied", 2: "lead replied", 3: "lead unsubscribed", 4: "suppressed"}


# --- 4. editing a running flow -------------------------------------------------------------


def test_deleting_a_connected_follow_up_reroutes_to_everyone_else(conn, cfg, google, clock):
    f = Flow(conn, leads=20)
    f.variant(1, "B")
    x = f.variant(2, "X", after=["B"])
    f.start()
    f.run(cfg, google, clock, days=0)  # first emails out
    assert campaigns.delete_variant(conn, x) is None  # dry run only, so it can be deleted
    conn.commit()
    f.run(cfg, google, clock, days=1)
    assert counts(f.paths(), 2) == {"A>A": 10, "B>A": 10}
    assert conn.execute("SELECT COUNT(*) FROM variant_links").fetchone()[0] == 0


def test_new_connection_takes_an_equal_share_and_keeps_the_rest_proportional(conn):
    f = Flow(conn, leads=1)
    f.variant(1, "B")
    f.link(2, "A", "A")
    f.variant(2, "Z", after=["A"])
    f.split(2, "A", A=70, Z=30)
    f.variant(2, "W", after=["A"])  # third option for A's leads
    shares = {r[0]["label"]: r[1] for r in campaigns.split(f.step(2), f.ids[(1, "A")])}
    assert shares == {"A": 47, "B": 20, "C": 33}  # labels: A, Z=B, W=C
    assert sum(shares.values()) == 100


def test_split_must_add_up_and_cover_every_option(conn):
    f = Flow(conn, leads=1)
    f.variant(1, "B")
    step = f.step(1)["id"]
    a, b = f.ids[(1, "A")], f.ids[(1, "B")]
    assert "add up to 90%" in campaigns.set_split(conn, step, None, {a: 60, b: 30})
    assert "changed meanwhile" in campaigns.set_split(conn, step, None, {a: 100})
    assert "between 0 and 100" in campaigns.set_split(conn, step, None, {a: 120, b: -20})
    assert campaigns.set_split(conn, step, None, {a: 55, b: 45}) is None


# --- 5. the numbers the UI shows -----------------------------------------------------------


def test_every_number_matches_what_happened(conn, cfg, google, clock):
    """Build a branched flow, run it part-way, and compare canvas/Results numbers with the DB."""
    f = Flow(conn, leads=100, steps=3, delay=2)
    f.variant(1, "B")
    f.split(1, None, A=60, B=40)
    f.link(2, "A", "A")
    f.variant(2, "Y", after=["B"])
    f.start(enroll=90)  # 10 leads stay out of the campaign
    f.run(cfg, google, clock, days=0)  # step 1 for all 90
    conn.execute("UPDATE messages SET replied_at = sent_at WHERE id IN (1, 2, 3)")
    conn.execute(
        "UPDATE leads SET status = 'replied' WHERE id IN (SELECT lead_id FROM messages WHERE id IN (1, 2, 3))"
    )
    conn.commit()
    f.run(cfg, google, clock, days=2)  # step 2 (3 repliers stop)

    campaign = campaigns.get(conn, f.id)
    seq = campaigns.sequence(conn, campaign)
    stats = campaigns.stats(conn, campaign)
    truth = Counter(
        (r["position"], r["label"])
        for r in conn.execute(
            "SELECT s.position, v.label FROM messages m JOIN steps s ON s.id = m.step_id"
            " JOIN variants v ON v.id = m.variant_id WHERE m.campaign_id = ?",
            (f.id,),
        )
    )
    # Per-card "sent" on the canvas
    for step in seq:
        for v in step["variants"]:
            assert v["sent"] == truth[(step["position"], v["label"])], (step["position"], v["label"])
    assert truth[(1, "A")] == 54 and truth[(1, "B")] == 36
    assert truth[(2, "A")] + truth[(2, "B")] == 87  # 90 - 3 who replied
    # Replies attributed to the email they answered
    assert sum(v["replies"] for v in seq[0]["variants"]) == 3
    # "N waiting" badges = active enrollments waiting for that step
    waiting = Counter(
        r[0]
        for r in conn.execute(
            "SELECT next_step FROM enrollments WHERE campaign_id = ? AND status = 'active'", (f.id,)
        )
    )
    assert {s["position"]: s["queued"] for s in seq} == {1: waiting[1], 2: waiting[2], 3: waiting[3]}
    assert waiting[3] == 87
    # Header tiles
    assert stats["enrolled"] == 90 and stats["sent"] == 177 and stats["reached"] == 90
    assert stats["active"] == 87 and stats["stopped"] == 3
    # Results: funnel per step and the A/B test on the first email
    from outreach_wise.web import _campaign_report

    report = _campaign_report(conn, campaign, db.parse("2026-10-21 10:00:00").tzinfo)
    assert [(r["position"], r["sent"], r["replies"]) for r in report["funnel"]] == [
        (1, 90, 3),
        (2, 87, 0),
        (3, 0, 0),
    ]
    assert sum(d["sent"] for d in report["daily"]) == 177
    first = next(t for t in campaigns.ab_results(conn, campaign) if t["position"] == 1)
    assert {v["label"]: v["sent"] for v in first["variants"]} == {"A": 54, "B": 36}


def test_split_percentages_shown_on_the_canvas(conn):
    """sequence() exposes the weights the canvas turns into % labels."""
    f = Flow(conn, leads=1)
    f.variant(1, "B")
    f.split(1, None, A=65, B=35)
    f.link(2, "A", "A")
    f.variant(2, "Z", after=["A"])
    f.split(2, "A", A=25, Z=75)
    seq = campaigns.sequence(conn, campaigns.get(conn, f.id))
    assert [v["weight"] for v in seq[0]["variants"]] == [65, 35]
    a_id = str(f.ids[(1, "A")])
    assert {v["label"]: v["link_weights"].get(a_id) for v in seq[1]["variants"]} == {"A": 25, "B": 75}


# --- 6. the same routing on real (fake-Gmail) sends ----------------------------------------


def test_live_sends_follow_the_branches_and_thread_correctly(conn, cfg, google, clock, account):
    f = Flow(conn, leads=6)
    f.variant(1, "B")
    f.link(2, "A", "A")
    f.variant(2, "Y", after=["B"])
    f.start()
    campaigns.go_live(conn, f.id)
    conn.commit()
    for _ in range(3 * 24 * 4):  # 3 days of worker ticks every 15 min (live sends are paced + windowed)
        sender.run_send_cycle(conn, cfg, google)
        clock.advance(minutes=15)
    by_thread = defaultdict(list)
    for sent in google.sent:
        body = (
            sent["mime"].get_payload(decode=True).decode()
            if not sent["mime"].is_multipart()
            else sent["mime"].get_payload(0).get_payload(decode=True).decode()
        )
        by_thread[sent["thread_id"]].append(body.split()[0])
    assert len(by_thread) == 6
    for tags in by_thread.values():
        assert tags in (["S1-A", "S2-A"], ["S1-B", "S2-Y"]), tags
