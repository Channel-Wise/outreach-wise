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

MAX_VARIANTS = 5


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
    cur = conn.execute("INSERT INTO campaigns (name, created_at) VALUES (?, ?)", (name, db.iso(db.now())))
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
    for step in steps:
        step["variants"] = [
            dict(v)
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


def add_variant(conn: sqlite3.Connection, step_id: int) -> str | None:
    variants = conn.execute("SELECT * FROM variants WHERE step_id = ? ORDER BY label", (step_id,)).fetchall()
    if len(variants) >= MAX_VARIANTS:
        return f"A step can have at most {MAX_VARIANTS} variants."
    used = {v["label"] for v in variants}
    label = next(letter for letter in ascii_uppercase if letter not in used)
    base = variants[0]
    conn.execute(
        "INSERT INTO variants (step_id, label, subject, body) VALUES (?, ?, ?, ?)",
        (step_id, label, base["subject"], base["body"]),
    )
    return None


def update_variant(
    conn: sqlite3.Connection, variant_id: int, subject: str, body: str, active: bool
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
    ORDER BY l.id
"""


def _eligible_sql(include_contacted: bool) -> str:
    return _ELIGIBLE.format(statuses="'new', 'contacted'" if include_contacted else "'new'")


def count_eligible(conn: sqlite3.Connection, campaign_id: int, include_contacted: bool = False) -> int:
    sql = f"SELECT COUNT(*) FROM ({_eligible_sql(include_contacted)})"
    return conn.execute(sql, {"campaign_id": campaign_id}).fetchone()[0]


def enroll(
    conn: sqlite3.Connection, campaign_id: int, limit: int | None = None, include_contacted: bool = False
) -> int:
    """Enroll eligible leads (never-contacted, not suppressed, not in another active sequence)."""
    stamp = db.iso(db.now())
    cur = conn.execute(
        "INSERT INTO enrollments (campaign_id, lead_id, status, next_step, next_send_at, created_at, updated_at)"
        f" SELECT :campaign_id, id, 'active', 1, :stamp, :stamp, :stamp FROM ({_eligible_sql(include_contacted)}"
        " LIMIT :limit)",
        {"campaign_id": campaign_id, "stamp": stamp, "limit": limit if limit else -1},
    )
    if cur.rowcount:
        db.log_event(conn, "enroll", f"Enrolled {cur.rowcount} leads", campaign_id=campaign_id)
    return cur.rowcount


def auto_enroll(conn: sqlite3.Connection) -> int:
    campaigns = conn.execute(
        "SELECT id FROM campaigns WHERE auto_enroll = 1 AND status = 'active' ORDER BY id"
    )
    return sum(enroll(conn, row["id"]) for row in campaigns.fetchall())


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


def ab_results(conn: sqlite3.Connection, campaign: sqlite3.Row) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id AS step_id, s.position, v.id, v.label, v.subject, v.active,
               COUNT(CASE WHEN m.status IN ('sent', 'dry_run') THEN 1 END) AS sent,
               COUNT(m.replied_at) AS replies
        FROM variants v JOIN steps s ON s.id = v.step_id
        LEFT JOIN messages m ON m.variant_id = v.id AND m.mode = ?
        WHERE s.campaign_id = ?
        GROUP BY v.id ORDER BY s.position, v.label
        """,
        (campaign["mode"], campaign["id"]),
    ).fetchall()
    steps: dict[int, dict] = {}
    for row in rows:
        step = steps.setdefault(row["step_id"], {"position": row["position"], "variants": [], "verdict": ""})
        rate = row["replies"] / row["sent"] if row["sent"] else 0.0
        step["variants"].append({**dict(row), "rate": rate})
    for step in steps.values():
        variants = step["variants"]
        if len(variants) < 2:
            continue
        ranked = sorted(variants, key=lambda v: v["rate"], reverse=True)
        best, runner_up = ranked[0], ranked[1]
        if min(v["sent"] for v in variants) < MIN_SAMPLE:
            step["verdict"] = (
                f"Collecting data — results are meaningful after {MIN_SAMPLE}+ sends per variant."
            )
        elif p_value(best["replies"], best["sent"], runner_up["replies"], runner_up["sent"]) < 0.05:
            best["winner"] = True
            step["verdict"] = f"Variant {best['label']} is winning with 95% confidence."
        else:
            step["verdict"] = "No significant difference yet."
    return list(steps.values())


# --- preview & approval ---------------------------------------------------------------------


def preview(conn: sqlite3.Connection, cfg: Config, campaign_id: int, footer: str, sample: int = 3) -> dict:
    """Render every variant for a few real leads, and count leads with missing variables."""
    steps = steps_with_variants(conn, campaign_id)
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
    for step in steps:
        for variant in step["variants"]:
            variant["renders"] = []
            for lead in sample_leads:
                variables = lead_variables(cfg, lead)
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
    return blockers


def go_live(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Discard dry-run history and restart the sequence for real. Returns active enrollments."""
    stamp = db.iso(db.now())
    conn.execute("DELETE FROM messages WHERE campaign_id = ? AND mode = 'dry_run'", (campaign_id,))
    cur = conn.execute(
        "UPDATE enrollments SET status = 'active', next_step = 1, next_send_at = ?, account_id = NULL,"
        " thread_id = NULL, last_message_id = NULL, last_subject = NULL, stop_reason = NULL, updated_at = ?"
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
