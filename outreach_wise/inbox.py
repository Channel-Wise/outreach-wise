"""Inbox polling: detects replies, bounces, unsubscribes and auto-replies, and stops sequences.

A message counts as related to a lead when it is in a thread we started, or comes from a
lead's address. Unrelated mail is only remembered by id (no content is stored).
"""

import html
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from . import db
from .config import Config
from .google import Google
from .security import decrypt

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@", "mail-daemon@")
BOUNCE_SUBJECTS = (
    "delivery status notification",
    "undeliverable",
    "undelivered mail",
    "mail delivery failed",
    "delivery failure",
    "returned mail",
    "failure notice",
    "message not delivered",
)
AUTO_REPLY_SUBJECTS = (
    "automatic reply",
    "auto-reply",
    "autoreply",
    "auto reply",
    "out of office",
    "out of the office",
    "away from the office",
    "on vacation",
    "otomatik yanıt",
)
UNSUBSCRIBE = re.compile(
    r"\b(unsubscribe|remove me|opt[ -]?out|stop (emailing|contacting|sending)|take me off|do not (email|contact))\b",
    re.IGNORECASE,
)
# Where quoted history starts in a reply; our own footer must not trigger an unsubscribe.
QUOTE_START = re.compile(
    r"\bOn .{0,200}?wrote:|-----\s*Original Message|\bFrom: .{0,100}\bSent: |^>", re.S | re.M
)
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
OUTCOME_STATUS = {"reply": "replied", "unsubscribe": "unsubscribed", "bounce": "bounced"}


def sender_of(message: dict) -> str:
    return parseaddr(message["headers"].get("from", ""))[1].lower()


def own_text(snippet: str) -> str:
    match = QUOTE_START.search(snippet)
    return snippet[: match.start()] if match else snippet


def classify(message: dict) -> str:
    headers = message["headers"]
    sender = sender_of(message)
    subject = headers.get("subject", "").strip().lower()
    if sender.startswith(BOUNCE_SENDERS) or subject.startswith(BOUNCE_SUBJECTS):
        return "bounce"
    automatic = (
        headers.get("auto-submitted", "no").strip().lower() != "no"
        or "x-autoreply" in headers
        or "x-autorespond" in headers
        or headers.get("precedence", "").strip().lower() == "auto_reply"
    )
    if automatic or subject.startswith(AUTO_REPLY_SUBJECTS):
        return "auto_reply"
    if UNSUBSCRIBE.search(subject) or UNSUBSCRIBE.search(own_text(html.unescape(message["snippet"]))):
        return "unsubscribe"
    return "reply"


def handle(conn: sqlite3.Connection, account: sqlite3.Row, message: dict, moment: datetime) -> str:
    """Classify one inbound message, record it, and apply its effect. Returns its kind."""
    sender = sender_of(message)
    match = conn.execute(
        "SELECT enrollment_id, lead_id FROM messages WHERE gmail_thread_id = ? AND account_id = ?"
        " AND mode = 'live' ORDER BY id DESC LIMIT 1",
        (message["thread_id"], account["id"]),
    ).fetchone()
    lead_id = match["lead_id"] if match else None
    enrollment_id = match["enrollment_id"] if match else None
    kind = classify(message)
    snippet = html.unescape(message["snippet"])

    if lead_id is None:
        # Not in one of our threads: match by the sender (or, for bounces, the failed address).
        candidates = EMAIL_IN_TEXT.findall(snippet) if kind == "bounce" else [sender]
        for email in candidates:
            row = conn.execute("SELECT id FROM leads WHERE email = ?", (email.lower(),)).fetchone()
            if row:
                lead_id = row["id"]
                break
        if lead_id is not None:
            latest = conn.execute(
                "SELECT enrollment_id FROM messages WHERE lead_id = ? AND mode = 'live' ORDER BY id DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
            enrollment_id = latest["enrollment_id"] if latest else None
    if lead_id is None:
        kind = "ignored"

    received = datetime.fromtimestamp(message["internal_date"], UTC) if message["internal_date"] else moment
    related = kind != "ignored"
    conn.execute(
        "INSERT OR IGNORE INTO inbound (gmail_id, account_id, thread_id, kind, from_email, subject, snippet,"
        " lead_id, enrollment_id, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message["id"],
            account["id"],
            message["thread_id"] if related else None,
            kind,
            sender if related else None,
            message["headers"].get("subject") if related else None,
            snippet if related else None,
            lead_id,
            enrollment_id,
            db.iso(received),
        ),
    )
    if kind in OUTCOME_STATUS:
        apply_outcome(conn, lead_id, kind, received, enrollment_id, snippet)
    elif kind == "auto_reply":
        db.log_event(
            conn,
            "auto_reply",
            f"Auto-reply from {sender} (sequence continues)",
            lead_id=lead_id,
            account_id=account["id"],
        )
    return kind


def apply_outcome(
    conn: sqlite3.Connection,
    lead_id: int,
    kind: str,
    at: datetime,
    enrollment_id: int | None = None,
    note: str = "",
) -> None:
    """A reply, unsubscribe or bounce: stop every active sequence for this lead."""
    status = OUTCOME_STATUS[kind]
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    stamp = db.iso(at)
    if not (lead["status"] == "unsubscribed" and kind == "reply"):
        conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (status, stamp, lead_id))
    if enrollment_id:
        # The sequence that got the reply counts it, even if it had already finished.
        conn.execute(
            "UPDATE enrollments SET status = ?, stop_reason = ?, updated_at = ?"
            " WHERE id = ? AND status IN ('active', 'completed')",
            (status, kind, stamp, enrollment_id),
        )
    conn.execute(
        "UPDATE enrollments SET status = ?, stop_reason = ?, updated_at = ? WHERE lead_id = ? AND status = 'active'",
        (status, kind, stamp, lead_id),
    )
    if kind == "reply" and enrollment_id:
        # Attribute the reply to the last email sent before it (for per-step / A/B stats).
        conn.execute(
            """
            UPDATE messages SET replied_at = ? WHERE id = (
                SELECT id FROM messages WHERE enrollment_id = ? AND mode = 'live' AND status = 'sent'
                ORDER BY sent_at <= ? DESC, sent_at DESC, id DESC LIMIT 1
            ) AND replied_at IS NULL
            """,
            (stamp, enrollment_id, stamp),
        )
    if kind in ("unsubscribe", "bounce"):
        db.suppress(conn, lead["email"], status)
    labels = {"reply": "Reply from", "unsubscribe": "Unsubscribe from", "bounce": "Bounce for"}
    text = f"{labels[kind]} {lead['email']}" + (f": “{note[:140]}”" if note and kind != "bounce" else "")
    db.log_event(conn, kind, text, lead_id=lead_id, level="info" if kind == "reply" else "warning")


def disable_account(conn: sqlite3.Connection, account: sqlite3.Row, error: Exception) -> None:
    conn.execute("UPDATE accounts SET enabled = 0, last_error = ? WHERE id = ?", (str(error), account["id"]))
    db.log_event(
        conn,
        "account",
        f"Inbox {account['email']} paused: {error}. Reconnect it in Settings.",
        level="error",
        account_id=account["id"],
    )
    conn.commit()


def _known(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    known = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            f"SELECT gmail_id FROM inbound WHERE gmail_id IN ({','.join('?' * len(chunk))})", chunk
        )
        known.update(row[0] for row in rows)
    return known


def poll(
    conn: sqlite3.Connection,
    cfg: Config,
    google: Google,
    account: sqlite3.Row,
    moment: datetime | None = None,
) -> Counter:
    """Look at everything that arrived since the last poll (with 1h overlap; ids are deduplicated)."""
    moment = moment or db.now()
    refresh = decrypt(cfg.secret_key, account["refresh_token"])
    since = db.parse(account["inbox_checked_at"] or account["created_at"]) - timedelta(hours=1)
    listed = google.list_messages(refresh, f"-from:me after:{int(since.timestamp())}")
    known = _known(conn, [m["id"] for m in listed])
    counts: Counter = Counter()
    for item in reversed(listed):  # oldest first
        if item["id"] in known:
            continue
        counts[handle(conn, account, google.message(refresh, item["id"]), moment)] += 1
        conn.commit()
    conn.execute(
        "UPDATE accounts SET inbox_checked_at = ?, last_error = NULL WHERE id = ?",
        (db.iso(moment), account["id"]),
    )
    conn.commit()
    return counts


def check_thread(
    conn: sqlite3.Connection, cfg: Config, google: Google, account: sqlite3.Row, enrollment: sqlite3.Row
) -> bool:
    """Process any new messages in this enrollment's thread. True if the sequence must stop."""
    refresh = decrypt(cfg.secret_key, account["refresh_token"])
    messages = [
        m for m in google.thread(refresh, enrollment["thread_id"]) if sender_of(m) != account["email"]
    ]
    known = _known(conn, [m["id"] for m in messages])
    for message in messages:
        if message["id"] not in known:
            handle(conn, account, message, db.now())
    conn.commit()
    status = conn.execute("SELECT status FROM enrollments WHERE id = ?", (enrollment["id"],)).fetchone()[
        "status"
    ]
    return status != "active"
