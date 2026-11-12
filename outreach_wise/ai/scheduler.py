"""When AI fields are written, and the gate that holds an email until they're ready.

1. plan(): for active enrollments in campaigns with a playbook whose next step is due within
   the lookahead window, pick the step's variant ahead of time and pin it. Only if that
   variant uses {{ai_*}} and the lead has no fields for this playbook yet, queue a `pending`
   row. Fields are per (lead, playbook), so a lead reused in another campaign costs nothing.
2. run(): research + write pending rows, the soonest-to-send first, within the daily limit and
   the monthly budget.
3. gate(): called by the sender before rendering. Approved fields (or, when the campaign
   doesn't require approval, drafts) are added to the template variables; otherwise the email
   waits. Waiting never uses an inbox's daily limit.
"""

import json
import sqlite3
from datetime import datetime, timedelta

from .. import db, sender
from ..config import Config
from . import generate
from .playbooks import ai_fields_used, load

HOLD = timedelta(hours=1)
HOLD_FAILED = timedelta(hours=6)


def _queue(conn: sqlite3.Connection, lead_id: int, playbook_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ai_fields (lead_id, playbook_id, status, created_at) VALUES (?, ?, 'pending', ?)",
        (lead_id, playbook_id, db.iso(db.now())),
    )


def _pin(conn: sqlite3.Connection, enrollment_id: int, variant_id: int, position: int) -> None:
    conn.execute(
        "UPDATE enrollments SET pinned_variant_id = ?, pinned_step = ? WHERE id = ?",
        (variant_id, position, enrollment_id),
    )


def plan(conn: sqlite3.Connection, settings: dict, moment: datetime | None = None, limit: int = 500) -> int:
    """Pin upcoming variants and queue the AI fields they need. Returns rows queued."""
    if not settings["ai_enabled"]:
        return 0
    moment = moment or db.now()
    horizon = moment + timedelta(hours=int(settings["ai_lookahead_hours"]))
    rows = conn.execute(
        """
        SELECT e.*, c.playbook_id FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
        WHERE e.status = 'active' AND c.status = 'active' AND c.playbook_id IS NOT NULL
          AND e.next_send_at <= ? AND (e.pinned_step IS NULL OR e.pinned_step != e.next_step)
        ORDER BY e.next_send_at, e.id LIMIT ?
        """,
        (db.iso(horizon), limit),
    ).fetchall()
    queued = 0
    for enrollment in rows:
        step = conn.execute(
            "SELECT * FROM steps WHERE campaign_id = ? AND position = ?",
            (enrollment["campaign_id"], enrollment["next_step"]),
        ).fetchone()
        if not step:
            continue
        previous = (
            sender._previous_variant(conn, enrollment, step["position"]) if step["position"] > 1 else None
        )
        variant = sender._pick_variant(conn, step["id"], previous["id"] if previous else None)
        if not variant:
            continue
        # Pin right away so the next pick in this batch sees it in the A/B tally.
        _pin(conn, enrollment["id"], variant["id"], step["position"])
        if ai_fields_used(variant["subject"] + variant["body"]):
            before = conn.total_changes
            _queue(conn, enrollment["lead_id"], enrollment["playbook_id"])
            queued += conn.total_changes > before
    conn.commit()
    return queued


def next_pending(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The pending row whose email is due soonest (rows not tied to an active enrollment last)."""
    return conn.execute(
        """
        SELECT a.*, (SELECT MIN(e.next_send_at) FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
                     WHERE e.lead_id = a.lead_id AND c.playbook_id = a.playbook_id AND e.status = 'active') AS due
        FROM ai_fields a WHERE a.status = 'pending'
        ORDER BY due IS NULL, due, a.id LIMIT 1
        """
    ).fetchone()


def run(
    conn: sqlite3.Connection, cfg: Config, settings: dict, max_items: int = 1, provider=None, client=None
) -> int:
    """Write up to max_items pending rows. Returns how many were processed."""
    if not settings["ai_enabled"]:
        return 0
    done = 0
    while done < max_items:
        reason = generate.over_limits(conn, settings)
        if reason:
            if db.get_kv(conn, "ai_paused") != reason:
                db.set_kv(conn, "ai_paused", reason)
                db.log_event(conn, "ai", f"AI research paused: {reason}", level="warning")
                conn.commit()
            break
        row = next_pending(conn)
        if not row:
            break
        if db.get_kv(conn, "ai_paused"):
            conn.execute("DELETE FROM kv WHERE key = 'ai_paused'")
        generate.record_spend(conn, 0, count=1)
        conn.commit()
        generate.run_one(conn, cfg, settings, row["id"], provider=provider, client=client)
        done += 1
    return done


def recover(conn: sqlite3.Connection) -> None:
    """Rows left 'researching' by a crash or restart go back to the queue."""
    conn.execute("UPDATE ai_fields SET status = 'pending' WHERE status = 'researching'")
    conn.commit()


def release_held(conn: sqlite3.Connection, lead_id: int, playbook_id: int) -> None:
    """After approval: emails that were waiting for these fields can go out right away."""
    now = db.now()
    conn.execute(
        """
        UPDATE enrollments SET next_send_at = ?
        WHERE lead_id = ? AND status = 'active' AND pinned_step = next_step AND next_send_at <= ?
          AND campaign_id IN (SELECT id FROM campaigns WHERE playbook_id = ?)
        """,
        (db.iso(now), lead_id, db.iso(now + HOLD), playbook_id),
    )


def gate(
    conn: sqlite3.Connection,
    settings: dict,
    enrollment: sqlite3.Row,
    step: sqlite3.Row,
    variant: sqlite3.Row,
    lead: sqlite3.Row,
    variables: dict,
    moment: datetime,
) -> str | None:
    """None: go ahead (variables now include ai_*). "held": try again later. Anything else:
    the reason to stop this enrollment."""
    needed = ai_fields_used(variant["subject"] + variant["body"])
    if not needed:
        return None
    campaign = conn.execute(
        "SELECT playbook_id, require_approval FROM campaigns WHERE id = ?", (enrollment["campaign_id"],)
    ).fetchone()
    if not campaign["playbook_id"]:
        return f"template uses {{{{ai_{sorted(needed)[0]}}}}} but the campaign has no playbook"
    playbook = load(conn, campaign["playbook_id"])
    if playbook is None:
        return "the campaign's playbook is missing or invalid"
    unknown = needed - {f.name for f in playbook.fields}
    if unknown:
        return f"the playbook doesn't write ai_{sorted(unknown)[0]}"

    def hold(delay: timedelta) -> str:
        _pin(conn, enrollment["id"], variant["id"], step["position"])
        conn.execute(
            "UPDATE enrollments SET next_send_at = ?, updated_at = ? WHERE id = ?",
            (db.iso(moment + delay), db.iso(moment), enrollment["id"]),
        )
        return "held"

    if not settings["ai_enabled"]:
        return hold(HOLD)  # switched off for now: wait, don't drop the lead
    row = conn.execute(
        "SELECT * FROM ai_fields WHERE lead_id = ? AND playbook_id = ?", (lead["id"], campaign["playbook_id"])
    ).fetchone()
    if row is None:
        _queue(conn, lead["id"], campaign["playbook_id"])
        return hold(HOLD)
    ready = row["status"] == "approved" or (
        row["status"] == "needs_review" and not campaign["require_approval"]
    )
    if ready:
        for name, value in json.loads(row["vals"]).items():
            variables[f"ai_{name}"] = value
        return None
    if row["status"] == "skipped":
        return f"AI skipped this lead: {row['skip_reason'] or 'no reason given'}"
    return hold(HOLD_FAILED if row["status"] == "failed" else HOLD)
