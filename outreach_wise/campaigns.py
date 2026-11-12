"""Campaigns: sequences (steps), A/B variants, enrollment, stats and the go-live approval."""

import json
import math
import sqlite3
from string import ascii_uppercase

from . import db
from .config import Config
from .rendering import MissingVariable, render, required_variables, single_line
from .security import unsubscribe_token

FIRST_SUBJECT = "Quick question about {{channel_name|your channel}}"
FIRST_BODY = """Hi {{first_name|there}},

I came across {{channel_name|your channel}} and loved what you're building.

<one or two sentences on why you're reaching out>

Would you be open to a quick chat this week?

Best,
<your name>"""
FOLLOWUP_BODY = """Hi {{first_name|there}},

Just bumping this up in case it got buried. Happy to share more details if it's useful.

Best,
<your name>"""

MAX_VARIANTS = 8


# --- template variables ---------------------------------------------------------------------


def unsubscribe_url(cfg: Config, lead_id: int) -> str:
    return f"{cfg.base_url}/u/{unsubscribe_token(cfg.secret_key, lead_id)}"


def lead_variables(cfg: Config, lead: sqlite3.Row) -> dict:
    fields = json.loads(lead["fields"])
    return {**fields, "email": lead["email"], "unsubscribe_url": unsubscribe_url(cfg, lead["id"])}


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


# --- campaigns, steps, variants -------------------------------------------------------------


def get(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()


def create(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO campaigns (name, created_at, require_approval) VALUES (?, ?, ?)",
        (name, db.iso(db.now()), int(db.get_settings(conn)["ai_require_approval"])),
    )
    campaign_id = cur.lastrowid
    add_step(conn, campaign_id, delay_days=0, subject=FIRST_SUBJECT, body=FIRST_BODY)
    add_step(conn, campaign_id, delay_days=3)
    db.log_event(conn, "campaign", f'Created campaign "{name}"', campaign_id=campaign_id)
    return campaign_id


def add_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    delay_days: int = 3,
    subject: str = "",
    body: str = FOLLOWUP_BODY,
) -> int:
    position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM steps WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO steps (campaign_id, position, delay_days) VALUES (?, ?, ?)",
        (campaign_id, position, delay_days),
    )
    conn.execute(
        "INSERT INTO variants (step_id, label, subject, body) VALUES (?, 'A', ?, ?)",
        (cur.lastrowid, subject, body),
    )
    return cur.lastrowid


def steps_with_variants(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    steps = [
        dict(s)
        for s in conn.execute("SELECT * FROM steps WHERE campaign_id = ? ORDER BY position", (campaign_id,))
    ]
    links: dict[int, dict[int, int]] = {}
    for variant_id, parent_id, weight in conn.execute(
        "SELECT l.variant_id, l.parent_id, l.weight FROM variant_links l JOIN variants v ON v.id = l.variant_id"
        " JOIN steps s ON s.id = v.step_id WHERE s.campaign_id = ? ORDER BY l.parent_id",
        (campaign_id,),
    ):
        links.setdefault(variant_id, {})[parent_id] = weight
    for step in steps:
        step["variants"] = [
            dict(v)
            | {
                "parents": list(links.get(v["id"], {})),
                # JSON object keys are strings; the canvas reads link_weights[String(parentId)].
                "link_weights": {str(k): w for k, w in links.get(v["id"], {}).items()},
            }
            for v in conn.execute("SELECT * FROM variants WHERE step_id = ? ORDER BY label", (step["id"],))
        ]
    return steps


def _live_messages(conn: sqlite3.Connection, column: str, value: int) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE {column} = ? AND mode = 'live'", (value,)
    ).fetchone()[0]


def delete_step(conn: sqlite3.Connection, step_id: int) -> str | None:
    """Returns an error message, or None on success."""
    step = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    if not step:
        return "Step not found."
    if (
        conn.execute("SELECT COUNT(*) FROM steps WHERE campaign_id = ?", (step["campaign_id"],)).fetchone()[0]
        == 1
    ):
        return "A campaign needs at least one step."
    if _live_messages(conn, "step_id", step_id):
        return "This step has already been sent to leads; it can't be deleted."
    conn.execute("DELETE FROM steps WHERE id = ?", (step_id,))
    # Close the gap in positions (two passes to respect the UNIQUE constraint).
    args = (step["campaign_id"], step["position"])
    conn.execute("UPDATE steps SET position = -position WHERE campaign_id = ? AND position > ?", args)
    conn.execute("UPDATE steps SET position = -position - 1 WHERE campaign_id = ? AND position < 0", args[:1])
    conn.execute(
        "UPDATE enrollments SET next_step = next_step - 1 WHERE campaign_id = ? AND next_step > ?", args
    )
    if step["position"] == 1:
        conn.execute("UPDATE steps SET delay_days = 0 WHERE campaign_id = ? AND position = 1", args[:1])
    return None


def _previous_step_variant(conn: sqlite3.Connection, step_id: int, variant_id: int) -> bool:
    """Is variant_id a variant of the step right before step_id (same campaign)?"""
    return (
        conn.execute(
            "SELECT 1 FROM variants v JOIN steps p ON p.id = v.step_id JOIN steps s ON s.id = ?"
            " WHERE v.id = ? AND p.campaign_id = s.campaign_id AND p.position = s.position - 1",
            (step_id, variant_id),
        ).fetchone()
        is not None
    )


def add_variant(conn: sqlite3.Connection, step_id: int, parent_id: int | None = None) -> str | None:
    """Add an A/B variant; with parent_id it is linked to (only sent after) that previous variant."""
    variants = conn.execute("SELECT * FROM variants WHERE step_id = ? ORDER BY label", (step_id,)).fetchall()
    if len(variants) >= MAX_VARIANTS:
        return f"A step can have at most {MAX_VARIANTS} variants."
    if parent_id is not None and not _previous_step_variant(conn, step_id, parent_id):
        return "That variant isn't part of the previous step."
    used = {v["label"] for v in variants}
    label = next(letter for letter in ascii_uppercase if letter not in used)
    # Start from a follow-up already linked to the same parent if there is one, else the first variant.
    sibling = conn.execute(
        "SELECT v.* FROM variants v JOIN variant_links l ON l.variant_id = v.id"
        " WHERE v.step_id = ? AND l.parent_id = ? ORDER BY v.label LIMIT 1",
        (step_id, parent_id),
    ).fetchone()
    base = sibling or variants[0]
    cur = conn.execute(
        "INSERT INTO variants (step_id, label, subject, body) VALUES (?, ?, ?, ?)",
        (step_id, label, base["subject"], base["body"]),
    )
    if parent_id is not None:
        conn.execute(
            "INSERT INTO variant_links (variant_id, parent_id) VALUES (?, ?)", (cur.lastrowid, parent_id)
        )
    _rebalance(conn, step_id, cur.lastrowid, parent_id)
    return None


def link(conn: sqlite3.Connection, variant_id: int, parent_id: int, connect: bool = True) -> str | None:
    """Connect (or disconnect) a follow-up variant to a variant of the previous step.

    A follow-up can be linked to any number of previous variants; one without links goes to
    everyone whose previous variant has no linked follow-up.
    """
    variant = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if not variant:
        return "Variant not found."
    if not _previous_step_variant(conn, variant["step_id"], parent_id):
        return "You can only connect an email to the follow-up right after it."
    if connect:
        cur = conn.execute(
            "INSERT OR IGNORE INTO variant_links (variant_id, parent_id) VALUES (?, ?)",
            (variant_id, parent_id),
        )
        if cur.rowcount:
            _rebalance(conn, variant["step_id"], variant_id, parent_id)
    else:
        conn.execute(
            "DELETE FROM variant_links WHERE variant_id = ? AND parent_id = ?", (variant_id, parent_id)
        )
    return None


def update_variant(
    conn: sqlite3.Connection,
    variant_id: int,
    subject: str,
    body: str,
    active: bool,
) -> str | None:
    variant = conn.execute(
        "SELECT v.*, s.position FROM variants v JOIN steps s ON s.id = v.step_id WHERE v.id = ?",
        (variant_id,),
    ).fetchone()
    if not variant:
        return "Variant not found."
    subject = single_line(subject)
    if variant["position"] == 1 and not subject:
        return "The first email needs a subject."
    if not body.strip():
        return "The email body can't be empty."
    if not active:
        others = conn.execute(
            "SELECT COUNT(*) FROM variants WHERE step_id = ? AND id != ? AND active = 1",
            (variant["step_id"], variant_id),
        ).fetchone()[0]
        if not others:
            return "Each step needs at least one active variant."
    conn.execute(
        "UPDATE variants SET subject = ?, body = ?, active = ? WHERE id = ?",
        (subject, body.replace("\r\n", "\n"), int(active), variant_id),
    )
    return None


def delete_variant(conn: sqlite3.Connection, variant_id: int) -> str | None:
    variant = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if not variant:
        return "Variant not found."
    siblings = conn.execute(
        "SELECT COUNT(*) FROM variants WHERE step_id = ? AND active = 1 AND id != ?",
        (variant["step_id"], variant_id),
    ).fetchone()[0]
    if not siblings:
        return "Each step needs at least one active variant."
    if _live_messages(conn, "variant_id", variant_id):
        return "This variant has been sent; deactivate it instead so its results are kept."
    conn.execute("DELETE FROM variants WHERE id = ?", (variant_id,))
    return None


def delete(conn: sqlite3.Connection, campaign_id: int) -> str | None:
    if _live_messages(conn, "campaign_id", campaign_id):
        return "This campaign has sent real emails; pause it instead to keep its history."
    conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    return None


# --- enrollment -----------------------------------------------------------------------------

_ELIGIBLE = """
    SELECT l.id FROM leads l
    WHERE l.status IN ({statuses})
      AND NOT EXISTS (
          SELECT 1 FROM enrollments e
          WHERE e.lead_id = l.id AND (e.campaign_id = :campaign_id OR e.status = 'active'))
      AND NOT EXISTS (
          SELECT 1 FROM suppressions s
          WHERE s.value = l.email OR s.value = '@' || substr(l.email, instr(l.email, '@') + 1))
      AND (:list_id IS NULL OR EXISTS (
          SELECT 1 FROM lead_list_members m WHERE m.lead_id = l.id AND m.list_id = :list_id))
    ORDER BY l.id
"""


def _eligible_sql(include_contacted: bool) -> str:
    return _ELIGIBLE.format(statuses="'new', 'contacted'" if include_contacted else "'new'")


def count_eligible(
    conn: sqlite3.Connection, campaign_id: int, include_contacted: bool = False, list_id: int | None = None
) -> int:
    sql = f"SELECT COUNT(*) FROM ({_eligible_sql(include_contacted)})"
    return conn.execute(sql, {"campaign_id": campaign_id, "list_id": list_id}).fetchone()[0]


def eligible_leads(
    conn: sqlite3.Connection, campaign_id: int, include_contacted: bool = True, limit: int = 5000
) -> list[sqlite3.Row]:
    """Leads that could be enrolled right now (from any list), in sheet order, for the lead picker."""
    return conn.execute(
        f"SELECT l.* FROM leads l WHERE l.id IN ({_eligible_sql(include_contacted)}) ORDER BY l.id LIMIT :limit",
        {"campaign_id": campaign_id, "limit": limit, "list_id": None},
    ).fetchall()


def enroll(
    conn: sqlite3.Connection,
    campaign_id: int,
    limit: int | None = None,
    include_contacted: bool = False,
    *,
    lead_ids: list[int] | None = None,
    shuffle: bool = False,
    list_id: int | None = None,
) -> int:
    """Enroll eligible leads (never-contacted, not suppressed, not in another active sequence).

    lead_ids restricts enrollment to those leads (ineligible ones are silently skipped);
    shuffle picks `limit` leads at random instead of the first ones in sheet order;
    list_id only takes leads from that list.
    """
    stamp = db.iso(db.now())
    params: dict = {
        "campaign_id": campaign_id,
        "stamp": stamp,
        "limit": limit if limit else -1,
        "list_id": list_id,
    }
    source = f"SELECT id FROM ({_eligible_sql(include_contacted)})"
    if lead_ids is not None:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS picked (id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM picked")
        conn.executemany("INSERT OR IGNORE INTO picked (id) VALUES (?)", [(int(i),) for i in lead_ids])
        source += " WHERE id IN (SELECT id FROM picked)"
    if shuffle:
        source += " ORDER BY random()"
    cur = conn.execute(
        "INSERT INTO enrollments (campaign_id, lead_id, status, next_step, next_send_at, created_at, updated_at)"
        f" SELECT :campaign_id, id, 'active', 1, :stamp, :stamp, :stamp FROM ({source} LIMIT :limit)",
        params,
    )
    if lead_ids is not None:
        conn.execute("DELETE FROM picked")
    if cur.rowcount:
        how = "hand-picked" if lead_ids is not None else "random" if shuffle else "in sheet order"
        if list_id:
            name = conn.execute("SELECT name FROM lead_lists WHERE id = ?", (list_id,)).fetchone()
            how += f", from {name['name']}" if name else ""
        db.log_event(conn, "enroll", f"Enrolled {cur.rowcount} leads ({how})", campaign_id=campaign_id)
    return cur.rowcount


def unenroll(conn: sqlite3.Connection, campaign_id: int, lead_ids: list[int]) -> int:
    """Take leads out of a campaign. Leads that already got a real email keep their history
    (the enrollment is stopped); everyone else is removed as if never enrolled."""
    removed = 0
    for lead_id in {int(i) for i in lead_ids}:
        enrollment = conn.execute(
            "SELECT * FROM enrollments WHERE campaign_id = ? AND lead_id = ?", (campaign_id, lead_id)
        ).fetchone()
        if not enrollment:
            continue
        if _live_messages(conn, "enrollment_id", enrollment["id"]):
            if enrollment["status"] != "active":
                continue
            conn.execute(
                "UPDATE enrollments SET status = 'stopped', stop_reason = 'removed from campaign', updated_at = ?"
                " WHERE id = ?",
                (db.iso(db.now()), enrollment["id"]),
            )
        else:
            conn.execute("DELETE FROM enrollments WHERE id = ?", (enrollment["id"],))
        removed += 1
    if removed:
        db.log_event(conn, "enroll", f"Removed {removed} leads from the campaign", campaign_id=campaign_id)
    return removed


def auto_enroll(conn: sqlite3.Connection) -> int:
    """New leads join active auto-enroll campaigns: from the campaign's list, or any list."""
    campaigns = conn.execute(
        "SELECT id, list_id FROM campaigns WHERE auto_enroll = 1 AND status = 'active' ORDER BY id"
    )
    return sum(enroll(conn, row["id"], list_id=row["list_id"]) for row in campaigns.fetchall())


# --- stats ----------------------------------------------------------------------------------


def stats(conn: sqlite3.Connection, campaign: sqlite3.Row) -> dict:
    cid, mode = campaign["id"], campaign["mode"]
    by_status = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM enrollments WHERE campaign_id = ? GROUP BY status", (cid,)
        ).fetchall()
    )
    sent, reached, failed = conn.execute(
        "SELECT SUM(status IN ('sent', 'dry_run')), COUNT(DISTINCT CASE WHEN status IN ('sent', 'dry_run')"
        " THEN lead_id END), SUM(status IN ('failed', 'unknown'))"
        " FROM messages WHERE campaign_id = ? AND mode = ?",
        (cid, mode),
    ).fetchone()
    replied = by_status.get("replied", 0)
    return {
        "enrolled": sum(by_status.values()),
        "active": by_status.get("active", 0),
        "completed": by_status.get("completed", 0),
        "replied": replied,
        "bounced": by_status.get("bounced", 0),
        "unsubscribed": by_status.get("unsubscribed", 0),
        "stopped": by_status.get("stopped", 0),
        "sent": sent or 0,
        "reached": reached or 0,
        "failed": failed or 0,
        "reply_rate": replied / reached if reached else 0.0,
    }


def p_value(successes_a: int, n_a: int, successes_b: int, n_b: int) -> float:
    """Two-sided two-proportion z-test."""
    if not n_a or not n_b:
        return 1.0
    pooled = (successes_a + successes_b) / (n_a + n_b)
    if pooled in (0, 1):
        return 1.0
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    z = abs(successes_a / n_a - successes_b / n_b) / se
    return math.erfc(z / math.sqrt(2))


MIN_SAMPLE = 30


def targets(step: dict, previous_variant_id: int | None) -> list[dict]:
    """Active variants of `step` that a lead who got `previous_variant_id` can receive.

    previous_variant_id None = the first email (every active variant).
    """
    active = [v for v in step["variants"] if v["active"]]
    if previous_variant_id is None:
        return active
    linked = [v for v in active if previous_variant_id in v["parents"]]
    return linked or [v for v in active if not v["parents"]]


def split(step: dict, previous_variant_id: int | None) -> list[tuple[dict, int]]:
    """(variant, weight) for the leads coming from `previous_variant_id`.

    A connected follow-up uses the weight of that connection; the first email and "everyone
    else" follow-ups use the variant's own weight. Percentages are weight / sum of weights.
    """
    pairs = []
    for v in targets(step, previous_variant_id):
        linked = previous_variant_id is not None and previous_variant_id in v["parents"]
        pairs.append((v, v["link_weights"][str(previous_variant_id)] if linked else v["weight"]))
    return pairs


def _percents(weights: list[int], total: int = 100) -> list[int]:
    """Whole numbers proportional to `weights` that add up to exactly `total` (largest remainder)."""
    if not sum(weights):
        weights = [1] * len(weights)
    raw = [total * w / sum(weights) for w in weights]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[: total - sum(out)]:
        out[i] += 1
    return out


def _rebalance(conn: sqlite3.Connection, step_id: int, new_variant_id: int, parent_id: int | None) -> None:
    """Give a newly added variant/connection an equal share; the others keep their proportions."""
    campaign_id = conn.execute("SELECT campaign_id FROM steps WHERE id = ?", (step_id,)).fetchone()[0]
    step = next(s for s in steps_with_variants(conn, campaign_id) if s["id"] == step_id)
    if parent_id is not None or step["position"] == 1:
        pairs = split(step, parent_id)
    else:  # an "everyone else" follow-up shares the pool of unconnected variants
        pairs = [(v, v["weight"]) for v in step["variants"] if v["active"] and not v["parents"]]
    others = [(v, w) for v, w in pairs if v["id"] != new_variant_id]
    if not others or len(others) == len(pairs):
        return
    if len({w for _, w in others}) == 1:  # was an even split: keep it even
        _write_split(conn, pairs, parent_id, {v["id"]: 100 for v, _ in pairs})
        return
    share = round(100 / len(pairs))
    percents = dict(
        zip([v["id"] for v, _ in others], _percents([w for _, w in others], 100 - share), strict=True)
    )
    percents[new_variant_id] = share
    _write_split(conn, pairs, parent_id, percents)


def _write_split(
    conn: sqlite3.Connection, pairs: list, parent_id: int | None, percents: dict[int, int]
) -> None:
    """Store shares: on the connection for a linked follow-up, else on the variant itself."""
    for v, _ in pairs:
        if parent_id is not None and parent_id in v["parents"]:
            conn.execute(
                "UPDATE variant_links SET weight = ? WHERE variant_id = ? AND parent_id = ?",
                (percents[v["id"]], v["id"], parent_id),
            )
        else:
            conn.execute("UPDATE variants SET weight = ? WHERE id = ?", (percents[v["id"]], v["id"]))


def set_split(
    conn: sqlite3.Connection, step_id: int, previous_variant_id: int | None, percents: dict[int, int]
) -> str | None:
    """Set how leads coming from `previous_variant_id` (None = first email) are split across
    the follow-ups they can get. `percents` maps variant id -> share, must cover every target."""
    step_row = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    if not step_row:
        return "Step not found."
    step = next(s for s in steps_with_variants(conn, step_row["campaign_id"]) if s["id"] == step_id)
    if previous_variant_id is not None and not _previous_step_variant(conn, step_id, previous_variant_id):
        return "That variant isn't part of the previous step."
    pairs = split(step, previous_variant_id)
    if {v["id"] for v, _ in pairs} != set(percents):
        return "The split changed meanwhile — reload and try again."
    if any(not 0 <= p <= 100 for p in percents.values()):
        return "Each share must be between 0 and 100%."
    if sum(percents.values()) != 100:
        return f"The shares add up to {sum(percents.values())}%, not 100%."
    _write_split(conn, pairs, previous_variant_id, percents)
    return None


def _variant_counts(conn: sqlite3.Connection, campaign: sqlite3.Row) -> dict[int, tuple[int, int]]:
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT m.variant_id, COUNT(CASE WHEN m.status IN ('sent', 'dry_run') THEN 1 END), COUNT(m.replied_at)"
            " FROM messages m WHERE m.campaign_id = ? AND m.mode = ? GROUP BY m.variant_id",
            (campaign["id"], campaign["mode"]),
        )
    }


def ab_results(conn: sqlite3.Connection, campaign: sqlite3.Row) -> list[dict]:
    """A/B tests: the variants of a step that receive the same leads compete with each other.

    For the first email that is every variant; for a follow-up it is, per previous variant, the
    follow-ups those leads can get (previous variants with the same options are merged).
    """
    counts = _variant_counts(conn, campaign)
    tests = []
    previous: list[dict] = []
    for step in steps_with_variants(conn, campaign["id"]):
        groups: dict[tuple, list[str]] = {}
        if step["position"] == 1:
            groups[tuple(v["id"] for v in step["variants"])] = []
        for parent in previous:
            if parent["active"]:
                ids = tuple(v["id"] for v in targets(step, parent["id"]))
                if ids:
                    groups.setdefault(ids, []).append(parent["label"])
        for ids, parents in groups.items():
            variants = []
            for v in step["variants"]:
                if v["id"] in ids:
                    sent, replies = counts.get(v["id"], (0, 0))
                    variants.append(
                        {**v, "sent": sent, "replies": replies, "rate": replies / sent if sent else 0.0}
                    )
            test = {
                "position": step["position"],
                "after_label": ", ".join(parents) or None,
                "variants": variants,
            }
            test["verdict"] = _verdict(variants)
            tests.append(test)
        previous = step["variants"]
    return tests


def _verdict(variants: list[dict]) -> str:
    if len(variants) < 2:
        return ""
    ranked = sorted(variants, key=lambda v: v["rate"], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if min(v["sent"] for v in variants) < MIN_SAMPLE:
        return f"Collecting data — results are meaningful after {MIN_SAMPLE}+ sends per variant."
    if p_value(best["replies"], best["sent"], runner_up["replies"], runner_up["sent"]) < 0.05:
        best["winner"] = True
        return f"Variant {best['label']} is winning with 95% confidence."
    return "No significant difference yet."


# --- preview & approval ---------------------------------------------------------------------


def annotate_routing(steps: list[dict]) -> None:
    """Who gets each email, for the preview page.

    Adds to every variant `routes`: [{"after": previous label or None, "pct": share, "linked": bool}]
    (one entry per previous variant whose leads can get it; "after" None = new leads, first email)
    and `unreached`; adds to every step `uncovered`: previous labels whose leads end here.
    """
    previous: list[dict] | None = None
    for step in steps:
        for v in step["variants"]:
            v["routes"], v["unreached"] = [], False
        step["uncovered"] = []
        parents = [None] if previous is None else [p for p in previous if p["active"]]
        for parent in parents:
            pairs = split(step, parent["id"] if parent else None)
            if not pairs:
                step["uncovered"].append(parent["label"])
                continue
            for (v, _), pct in zip(pairs, _percents([w for _, w in pairs]), strict=True):
                v["routes"].append(
                    {
                        "after": parent["label"] if parent else None,
                        "pct": pct,
                        "linked": parent is not None and parent["id"] in v["parents"],
                    }
                )
        if previous is not None:
            for v in step["variants"]:
                v["unreached"] = bool(v["active"] and not v["routes"])
        step["multi_previous"] = previous is not None and len(previous) > 1
        previous = step["variants"]


def preview(conn: sqlite3.Connection, cfg: Config, campaign_id: int, footer: str, sample: int = 3) -> dict:
    """Render every variant for a few real leads, and count leads with missing variables."""
    steps = steps_with_variants(conn, campaign_id)
    annotate_routing(steps)
    enrolled = conn.execute(
        "SELECT l.* FROM enrollments e JOIN leads l ON l.id = e.lead_id"
        " WHERE e.campaign_id = ? AND e.status = 'active' ORDER BY e.id",
        (campaign_id,),
    ).fetchall()
    sample_leads = (
        enrolled[:sample]
        or conn.execute(
            f"SELECT * FROM leads WHERE id IN ({_eligible_sql(False)} LIMIT {int(sample)})",
            {"campaign_id": campaign_id},
        ).fetchall()
    )

    first_subject = steps[0]["variants"][0]["subject"] if steps else ""
    playbook_id = conn.execute("SELECT playbook_id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()[0]
    for step in steps:
        for variant in step["variants"]:
            variant["renders"] = []
            for lead in sample_leads:
                variables = {**lead_variables(cfg, lead), **ai_preview_values(conn, playbook_id, lead["id"])}
                try:
                    subject_template = variant["subject"] or reply_subject(first_subject)
                    rendered = {
                        "email": lead["email"],
                        "subject": single_line(render(subject_template, variables)),
                        "body": render(variant["body"] + footer, variables),
                        "threaded": not variant["subject"],
                    }
                except MissingVariable as err:
                    rendered = {"email": lead["email"], "error": str(err)}
                variant["renders"].append(rendered)

    required = set()
    for step in steps:
        for variant in step["variants"]:
            if variant["active"]:
                required |= required_variables(variant["subject"] + variant["body"] + footer)
    required -= {"email", "unsubscribe_url"}
    required = {name for name in required if not name.startswith("ai_")}  # written by the playbook
    missing: dict[str, int] = {}
    affected = 0
    for lead in enrolled:
        fields = json.loads(lead["fields"])
        gaps = [name for name in required if not str(fields.get(name, "")).strip()]
        for name in gaps:
            missing[name] = missing.get(name, 0) + 1
        affected += bool(gaps)
    return {"steps": steps, "enrolled": len(enrolled), "missing": missing, "affected": affected}


def go_live_blockers(conn: sqlite3.Connection, campaign_id: int) -> list[str]:
    blockers = []
    if not conn.execute("SELECT 1 FROM accounts WHERE enabled = 1").fetchone():
        blockers.append("Connect at least one inbox in Settings.")
    first = conn.execute(
        "SELECT v.subject FROM variants v JOIN steps s ON s.id = v.step_id"
        " WHERE s.campaign_id = ? AND s.position = 1 AND v.active = 1",
        (campaign_id,),
    ).fetchall()
    if not first or any(not row["subject"].strip() for row in first):
        blockers.append("Every variant of the first email needs a subject.")
    blockers += ai_blockers(conn, campaign_id)
    return blockers


def ai_preview_values(conn: sqlite3.Connection, playbook_id: int | None, lead_id: int) -> dict:
    """ai_* values for previews: what the playbook wrote, or a visible placeholder."""
    if not playbook_id:
        return {}
    from .ai.playbooks import load  # noqa: PLC0415

    playbook = load(conn, playbook_id)
    if not playbook:
        return {}
    row = conn.execute(
        "SELECT status, vals FROM ai_fields WHERE lead_id = ? AND playbook_id = ?", (lead_id, playbook_id)
    ).fetchone()
    vals = json.loads(row["vals"]) if row and row["status"] in ("approved", "needs_review") else {}
    state = row["status"].replace("_", " ") if row else "not written yet"
    return {f.variable: vals.get(f.name) or f"[{f.variable}: {state}]" for f in playbook.fields}


def ai_blockers(conn: sqlite3.Connection, campaign_id: int) -> list[str]:
    from .ai.playbooks import ai_fields_used, load  # noqa: PLC0415

    used: set[str] = set()
    for row in conn.execute(
        "SELECT v.subject, v.body FROM variants v JOIN steps s ON s.id = v.step_id"
        " WHERE s.campaign_id = ? AND v.active = 1",
        (campaign_id,),
    ):
        used |= ai_fields_used(row["subject"] + row["body"])
    if not used:
        return []
    playbook_id = conn.execute("SELECT playbook_id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()[0]
    playbook = load(conn, playbook_id)
    if not playbook:
        return ["The emails use AI fields ({{ai_…}}). Pick a playbook in the campaign settings."]
    unknown = sorted(used - {f.name for f in playbook.fields})
    if unknown:
        return [f'The playbook "{playbook.name}" doesn\'t write ai_{unknown[0]}.']
    if not db.get_settings(conn)["ai_enabled"]:
        return ["Turn on AI personalization in Settings."]
    return []


def go_live(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Discard dry-run history and restart the sequence for real. Returns active enrollments."""
    stamp = db.iso(db.now())
    conn.execute("DELETE FROM messages WHERE campaign_id = ? AND mode = 'dry_run'", (campaign_id,))
    cur = conn.execute(
        "UPDATE enrollments SET status = 'active', next_step = 1, next_send_at = ?, account_id = NULL,"
        " thread_id = NULL, last_message_id = NULL, last_subject = NULL, stop_reason = NULL,"
        " pinned_variant_id = NULL, pinned_step = NULL, updated_at = ?"
        " WHERE campaign_id = ? AND status IN ('active', 'completed', 'stopped')",
        (stamp, stamp, campaign_id),
    )
    conn.execute(
        "UPDATE campaigns SET mode = 'live', status = 'active', approved_at = ? WHERE id = ?",
        (stamp, campaign_id),
    )
    name = get(conn, campaign_id)["name"]
    db.log_event(
        conn,
        "approval",
        f'Campaign "{name}" approved for LIVE sending to {cur.rowcount} enrolled leads',
        level="warning",
        campaign_id=campaign_id,
    )
    return cur.rowcount


# --- canvas editor payload ------------------------------------------------------------------


def sequence(conn: sqlite3.Connection, campaign: sqlite3.Row) -> list[dict]:
    """Steps with their variants, links, per-variant results and live counts, as JSON for the canvas."""
    tests = ab_results(conn, campaign)
    winners = {v["id"] for t in tests for v in t["variants"] if v.get("winner")}
    counts = _variant_counts(conn, campaign)
    queued = dict(
        conn.execute(
            "SELECT next_step, COUNT(*) FROM enrollments WHERE campaign_id = ? AND status = 'active'"
            " GROUP BY next_step",
            (campaign["id"],),
        ).fetchall()
    )
    steps = steps_with_variants(conn, campaign["id"])
    labels = {v["id"]: v["label"] for step in steps for v in step["variants"]}
    previous: list[dict] = []
    for step in steps:
        step["queued"] = queued.get(step["position"], 0)
        step["locked"] = bool(_live_messages(conn, "step_id", step["id"]))
        for variant in step["variants"]:
            sent, replies = counts.get(variant["id"], (0, 0))
            variant.update(
                parent_labels=[labels[p] for p in variant["parents"] if p in labels],
                sent=sent,
                replies=replies,
                rate=replies / sent if sent else 0.0,
                winner=variant["id"] in winners,
                locked=bool(_live_messages(conn, "variant_id", variant["id"])),
            )
        step["verdicts"] = [
            {"after_label": t["after_label"], "verdict": t["verdict"]}
            for t in tests
            if t["position"] == step["position"] and t["verdict"] and any(v["sent"] for v in t["variants"])
        ]
        # Leads who got a previous variant with no follow-up for them end the sequence here.
        active_parents = [p for p in previous if p["active"]]
        step["uncovered"] = [p["label"] for p in active_parents if not targets(step, p["id"])]
        # An "everyone else" follow-up nobody is left for (every previous variant has its own).
        reached = {v["id"] for p in active_parents for v in targets(step, p["id"])}
        for variant in step["variants"]:
            variant["unreached"] = bool(previous) and variant["active"] and variant["id"] not in reached
        previous = step["variants"]
    return steps
