"""The send cycle: picks due enrollments and sends (or dry-runs) each sequence step at most once.

Duplicate-send protection:
  1. A `messages` row is claimed under UNIQUE(enrollment_id, step_id) and committed
     *before* Gmail is called. A second claim for the same step is a no-op.
  2. If Gmail's answer is lost (timeout, crash), the row stays `sending`/becomes `unknown`
     and the sequence is stopped for manual review instead of being retried.
  3. Only one process runs the worker (see worker.py lease lock).
"""

import base64
import random
import sqlite3
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from zoneinfo import ZoneInfo

from . import campaigns, db, inbox
from .config import Config
from .google import AuthError, Google, GoogleError, SendStateUnknown
from .rendering import MissingVariable, render, single_line
from .security import decrypt

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=30)
STUCK_AFTER = timedelta(minutes=5)
# RFC 2606 / 6761 reserved names: never deliverable, used by demo data.
RESERVED_DOMAINS = ("example.com", "example.org", "example.net", "example", "test", "invalid", "localhost")


def is_reserved(email: str) -> bool:
    domain = db.email_domain(email)
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS)


# --- sending window -------------------------------------------------------------------------


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_window(moment: datetime, settings: dict) -> bool:
    local = moment.astimezone(ZoneInfo(settings["timezone"]))
    if local.weekday() not in settings["send_days"]:
        return False
    return _hhmm(settings["window_start"]) <= local.time() < _hhmm(settings["window_end"])


def window_end(moment: datetime, settings: dict) -> datetime:
    tz = ZoneInfo(settings["timezone"])
    local = moment.astimezone(tz)
    return datetime.combine(local.date(), _hhmm(settings["window_end"]), tzinfo=tz)


def day_start(moment: datetime, settings: dict) -> datetime:
    """Midnight in the configured timezone: daily limits reset then."""
    tz = ZoneInfo(settings["timezone"])
    return datetime.combine(moment.astimezone(tz).date(), time(0), tzinfo=tz)


def pacing_gap(moment: datetime, settings: dict, remaining: int) -> timedelta:
    """Delay before an inbox's next send: spreads the `remaining` daily quota evenly over
    what's left of today's window (so the last one still lands inside it), with jitter."""
    seconds_left = max((window_end(moment, settings) - moment).total_seconds(), 0)
    even = seconds_left / (max(remaining, 0) + 1)
    gap = max(settings["min_gap_seconds"], even * random.uniform(0.7, 1.3))
    return timedelta(seconds=int(gap))


def sent_today(conn: sqlite3.Connection, account_id: int, moment: datetime, settings: dict) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE account_id = ? AND mode = 'live'"
        " AND status IN ('sending', 'sent', 'unknown') AND created_at >= ?",
        (account_id, db.iso(day_start(moment, settings))),
    ).fetchone()[0]


# --- the cycle ------------------------------------------------------------------------------


def run_send_cycle(
    conn: sqlite3.Connection, cfg: Config, google: Google, moment: datetime | None = None
) -> int:
    """One worker tick. Returns the number of emails sent or dry-run."""
    moment = moment or db.now()
    settings = db.get_settings(conn)
    if settings["sending_paused"] or not in_window(moment, settings):
        return 0
    done = _dry_run_batch(conn, cfg, moment, settings)
    accounts = conn.execute("SELECT * FROM accounts WHERE enabled = 1 ORDER BY id").fetchall()
    for account in accounts:
        if account["next_send_at"] and db.parse(account["next_send_at"]) > moment:
            continue
        remaining = account["daily_limit"] - sent_today(conn, account["id"], moment, settings)
        if remaining <= 0:
            continue
        outcome = None
        for enrollment in due_enrollments(conn, moment, "live", account["id"]):
            outcome = process(conn, cfg, google, enrollment, account, moment, settings)
            if outcome != "skipped":
                break
        if outcome in ("sent", "failed", "unknown"):
            gap = pacing_gap(moment, settings, remaining - 1)
            if outcome == "failed":
                gap = max(gap, timedelta(minutes=5))
            conn.execute(
                "UPDATE accounts SET next_send_at = ? WHERE id = ?", (db.iso(moment + gap), account["id"])
            )
            conn.commit()
            done += outcome == "sent"
    return done


def _dry_run_batch(conn: sqlite3.Connection, cfg: Config, moment: datetime, settings: dict) -> int:
    """Dry-run campaigns go through every check but never call Gmail and aren't paced."""
    inboxes = conn.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 1").fetchone()[0]
    cap = settings["daily_limit"] * max(inboxes, 1)
    used = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE mode = 'dry_run' AND created_at >= ?",
        (db.iso(day_start(moment, settings)),),
    ).fetchone()[0]
    done, seen = 0, set()
    while used + done < cap:
        batch = due_enrollments(conn, moment, "dry_run", None, limit=min(cap - used - done, 100) + len(seen))
        batch = [e for e in batch if (e["id"], e["next_step"]) not in seen]
        if not batch:
            break
        for enrollment in batch:
            if used + done >= cap:
                break
            seen.add((enrollment["id"], enrollment["next_step"]))
            done += process(conn, cfg, None, enrollment, None, moment, settings) == "sent"
    return done


def due_enrollments(
    conn: sqlite3.Connection, moment: datetime, mode: str, account_id: int | None, limit: int = 25
) -> list[sqlite3.Row]:
    """Due enrollments, follow-ups first. A follow-up always goes out from the inbox that sent step 1."""
    return conn.execute(
        """
        SELECT e.* FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
        WHERE e.status = 'active' AND c.status = 'active' AND c.mode = :mode
          AND e.next_send_at <= :now AND (c.start_at IS NULL OR c.start_at <= :now)
          AND (:account_id IS NULL OR e.account_id IS NULL OR e.account_id = :account_id)
        ORDER BY e.next_step > 1 DESC, e.next_send_at, e.id
        LIMIT :limit
        """,
        {"mode": mode, "now": db.iso(moment), "account_id": account_id, "limit": limit},
    ).fetchall()


# --- one enrollment -------------------------------------------------------------------------


def _stop(conn: sqlite3.Connection, enrollment: sqlite3.Row, status: str, reason: str) -> None:
    conn.execute(
        "UPDATE enrollments SET status = ?, stop_reason = ?, updated_at = ? WHERE id = ?",
        (status, reason, db.iso(db.now()), enrollment["id"]),
    )


def _pick_variant(conn: sqlite3.Connection, step_id: int) -> sqlite3.Row | None:
    """Balanced A/B split: the active variant with the fewest messages so far."""
    return conn.execute(
        "SELECT v.* FROM variants v WHERE v.step_id = ? AND v.active = 1"
        " ORDER BY (SELECT COUNT(*) FROM messages m WHERE m.variant_id = v.id AND m.status != 'failed'), v.label"
        " LIMIT 1",
        (step_id,),
    ).fetchone()


def process(
    conn: sqlite3.Connection,
    cfg: Config,
    google: Google | None,
    enrollment: sqlite3.Row,
    account: sqlite3.Row | None,
    moment: datetime,
    settings: dict,
) -> str:
    """Send the next step of one enrollment. Returns sent | skipped | failed | unknown."""
    mode = "dry_run" if account is None else "live"
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (enrollment["lead_id"],)).fetchone()
    log = {"lead_id": lead["id"], "campaign_id": enrollment["campaign_id"]}

    # 1. Stop conditions, checked at send time (not just at enrollment time).
    if lead["status"] in ("replied", "bounced", "unsubscribed"):
        _stop(conn, enrollment, "stopped", f"lead {lead['status']}")
        conn.commit()
        return "skipped"
    if db.is_suppressed(conn, lead["email"]):
        _stop(conn, enrollment, "stopped", "suppressed")
        conn.commit()
        return "skipped"
    if mode == "live" and is_reserved(lead["email"]):
        _stop(conn, enrollment, "stopped", "reserved test domain, never sent")
        conn.commit()
        return "skipped"
    step = conn.execute(
        "SELECT * FROM steps WHERE campaign_id = ? AND position = ?",
        (enrollment["campaign_id"], enrollment["next_step"]),
    ).fetchone()
    if not step:
        _stop(conn, enrollment, "completed", None)
        conn.commit()
        return "skipped"
    variant = _pick_variant(conn, step["id"])
    if not variant:
        _stop(conn, enrollment, "stopped", f"step {step['position']} has no active variant")
        conn.commit()
        return "skipped"

    # 2. Render. Missing data stops this lead rather than sending a broken email.
    threaded = not variant["subject"].strip()
    if threaded and not enrollment["last_subject"]:
        _stop(conn, enrollment, "stopped", f"step {step['position']} has no subject and nothing to reply to")
        conn.commit()
        return "skipped"
    variables = campaigns.lead_variables(cfg, lead)
    try:
        subject = (
            campaigns.reply_subject(enrollment["last_subject"])
            if threaded
            else single_line(render(variant["subject"], variables))
        )
        body = render(variant["body"] + settings["footer"], variables)
    except MissingVariable as err:
        _stop(conn, enrollment, "stopped", f"template error: {err}")
        db.log_event(conn, "skip", f"Skipped {lead['email']}: {err}", level="warning", **log)
        conn.commit()
        return "skipped"

    # 3. Prepare the live email before claiming, so nothing but the API call sits between
    #    the claim and its result.
    raw = our_message_id = refresh = None
    if mode == "live":
        try:
            refresh = decrypt(cfg.secret_key, account["refresh_token"])
        except ValueError as err:
            inbox.disable_account(conn, account, err)
            return "failed"
        raw, our_message_id = build_mime(
            from_email=account["email"],
            from_name=settings["from_name"],
            to=lead["email"],
            subject=subject,
            body=body,
            in_reply_to=enrollment["last_message_id"] if threaded else None,
            unsubscribe_url=campaigns.unsubscribe_url(cfg, lead["id"]) if cfg.is_public else None,
        )
        # Just before a follow-up, re-check the thread so a fresh reply always wins.
        if enrollment["thread_id"]:
            try:
                if inbox.check_thread(conn, cfg, google, account, enrollment):
                    return "skipped"
            except AuthError as err:
                inbox.disable_account(conn, account, err)
                return "failed"
            except GoogleError as err:
                db.log_event(
                    conn, "error", f"Could not check thread before follow-up: {err}", level="warning", **log
                )
                conn.commit()
                return "failed"

    # 4. Claim the (enrollment, step) slot. Committed before any network call.
    stamp = db.iso(moment)
    root_subject = enrollment["last_subject"] if threaded else subject
    claimed = conn.execute(
        """
        INSERT INTO messages (enrollment_id, campaign_id, lead_id, step_id, variant_id, account_id, mode, status,
                              to_email, subject, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?, ?, ?)
        ON CONFLICT(enrollment_id, step_id) DO UPDATE SET
            status = 'sending', attempts = messages.attempts + 1, variant_id = excluded.variant_id,
            account_id = excluded.account_id, subject = excluded.subject, body = excluded.body,
            error = NULL, created_at = excluded.created_at
        WHERE messages.status = 'failed'
        RETURNING id, attempts
        """,
        (
            enrollment["id"],
            enrollment["campaign_id"],
            lead["id"],
            step["id"],
            variant["id"],
            account["id"] if account else None,
            mode,
            lead["email"],
            subject,
            body,
            stamp,
        ),
    ).fetchall()
    conn.commit()
    if not claimed:
        _resolve_existing(conn, enrollment, step)
        return "skipped"
    message_id, attempts = claimed[0]["id"], claimed[0]["attempts"]

    if mode == "dry_run":
        conn.execute("UPDATE messages SET status = 'dry_run', sent_at = ? WHERE id = ?", (stamp, message_id))
        _advance(conn, enrollment, moment, root_subject, None, None, None)
        db.log_event(
            conn, "dry_run", f"[dry run] Step {step['position']}{variant['label']} → {lead['email']}", **log
        )
        conn.commit()
        return "sent"

    # 5. Live send.
    try:
        sent = google.send(refresh, raw, enrollment["thread_id"] if threaded else None)
    except SendStateUnknown as err:
        conn.execute("UPDATE messages SET status = 'unknown', error = ? WHERE id = ?", (str(err), message_id))
        _stop(conn, enrollment, "stopped", "send not confirmed by Gmail; check the Sent folder (not retried)")
        db.log_event(
            conn,
            "error",
            f"Send to {lead['email']} not confirmed ({err}); sequence stopped to avoid a "
            "duplicate. Check the Sent folder.",
            level="error",
            account_id=account["id"],
            **log,
        )
        conn.commit()
        return "unknown"
    except GoogleError as err:
        conn.execute("UPDATE messages SET status = 'failed', error = ? WHERE id = ?", (str(err), message_id))
        if isinstance(err, AuthError):
            inbox.disable_account(conn, account, err)
        elif err.retryable and attempts < MAX_ATTEMPTS:
            conn.execute(
                "UPDATE enrollments SET next_send_at = ?, updated_at = ? WHERE id = ?",
                (db.iso(moment + RETRY_DELAY), stamp, enrollment["id"]),
            )
        else:
            _stop(conn, enrollment, "stopped", f"send failed: {err}")
        db.log_event(
            conn,
            "error",
            f"Send to {lead['email']} failed: {err}",
            level="error",
            account_id=account["id"],
            **log,
        )
        conn.commit()
        return "failed"

    # 6. Record success immediately, then fetch the real Message-ID for threading follow-ups.
    conn.execute(
        "UPDATE messages SET status = 'sent', sent_at = ?, gmail_id = ?, gmail_thread_id = ?, rfc_message_id = ?"
        " WHERE id = ?",
        (stamp, sent["id"], sent["threadId"], our_message_id, message_id),
    )
    conn.commit()
    rfc_message_id = our_message_id
    try:
        rfc_message_id = google.message(refresh, sent["id"])["headers"].get("message-id", our_message_id)
        conn.execute("UPDATE messages SET rfc_message_id = ? WHERE id = ?", (rfc_message_id, message_id))
    except GoogleError:
        pass  # threading still works through threadId
    _advance(conn, enrollment, moment, root_subject, sent["threadId"], rfc_message_id, account["id"])
    conn.execute(
        "UPDATE leads SET status = 'contacted', updated_at = ? WHERE id = ? AND status = 'new'",
        (stamp, lead["id"]),
    )
    db.log_event(
        conn,
        "sent",
        f"Sent step {step['position']}{variant['label']} to {lead['email']}",
        account_id=account["id"],
        **log,
    )
    conn.commit()
    return "sent"


def _resolve_existing(conn: sqlite3.Connection, enrollment: sqlite3.Row, step: sqlite3.Row) -> None:
    """The step was already claimed earlier. Repair state, never send again."""
    existing = conn.execute(
        "SELECT * FROM messages WHERE enrollment_id = ? AND step_id = ?", (enrollment["id"], step["id"])
    ).fetchone()
    if existing["status"] in ("sent", "dry_run"):
        # Crashed after the send was recorded but before the sequence advanced.
        when = db.parse(existing["sent_at"] or existing["created_at"])
        _advance(
            conn,
            enrollment,
            when,
            enrollment["last_subject"] or existing["subject"],
            existing["gmail_thread_id"],
            existing["rfc_message_id"],
            existing["account_id"],
        )
        if existing["mode"] == "live":
            conn.execute(
                "UPDATE leads SET status = 'contacted' WHERE id = ? AND status = 'new'",
                (enrollment["lead_id"],),
            )
    else:
        # 'sending' or 'unknown': we can't tell whether it went out.
        conn.execute("UPDATE messages SET status = 'unknown' WHERE id = ?", (existing["id"],))
        _stop(
            conn,
            enrollment,
            "stopped",
            "previous send attempt unconfirmed; check the Sent folder (not retried)",
        )
    conn.commit()


def _advance(
    conn: sqlite3.Connection,
    enrollment: sqlite3.Row,
    moment: datetime,
    subject: str,
    thread_id: str | None,
    message_id: str | None,
    account_id: int | None,
) -> None:
    next_position = enrollment["next_step"] + 1
    following = conn.execute(
        "SELECT delay_days FROM steps WHERE campaign_id = ? AND position = ?",
        (enrollment["campaign_id"], next_position),
    ).fetchone()
    next_send_at = moment + timedelta(days=following["delay_days"]) if following else moment
    conn.execute(
        """
        UPDATE enrollments SET next_step = ?, next_send_at = ?, status = ?, last_subject = ?,
            thread_id = COALESCE(?, thread_id), last_message_id = COALESCE(?, last_message_id),
            account_id = COALESCE(account_id, ?), updated_at = ?
        WHERE id = ?
        """,
        (
            next_position,
            db.iso(next_send_at),
            "active" if following else "completed",
            subject,
            thread_id,
            message_id,
            account_id,
            db.iso(moment),
            enrollment["id"],
        ),
    )


def build_mime(
    *,
    from_email: str,
    from_name: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[str, str]:
    """Plain-text email, base64url-encoded for the Gmail API. Returns (raw, Message-ID)."""
    message = EmailMessage()
    message["From"] = formataddr((from_name, from_email)) if from_name else from_email
    message["To"] = to
    message["Subject"] = single_line(subject)
    message["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=from_email.rsplit("@", 1)[-1])
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    unsubscribe = [f"<mailto:{from_email}?subject=unsubscribe>"]
    if unsubscribe_url:
        unsubscribe.append(f"<{unsubscribe_url}>")
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["List-Unsubscribe"] = ", ".join(unsubscribe)
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode(), message_id


# --- crash recovery -------------------------------------------------------------------------


def recover_interrupted_sends(conn: sqlite3.Connection, moment: datetime | None = None) -> int:
    """Called when the worker (re)acquires the lease. Never resends anything that may have gone out."""
    moment = moment or db.now()
    cutoff = db.iso(moment - STUCK_AFTER)
    conn.execute(
        "DELETE FROM messages WHERE status = 'sending' AND mode = 'dry_run' AND created_at < ?", (cutoff,)
    )
    stuck = conn.execute(
        "SELECT * FROM messages WHERE status = 'sending' AND mode = 'live' AND created_at < ?", (cutoff,)
    ).fetchall()
    for message in stuck:
        conn.execute(
            "UPDATE messages SET status = 'unknown', error = 'interrupted while sending' WHERE id = ?",
            (message["id"],),
        )
        conn.execute(
            "UPDATE enrollments SET status = 'stopped', stop_reason = ?, updated_at = ? WHERE id = ?",
            (
                "interrupted while sending; check the Sent folder (not retried)",
                db.iso(moment),
                message["enrollment_id"],
            ),
        )
        db.log_event(
            conn,
            "error",
            f"Send to {message['to_email']} was interrupted; sequence stopped to avoid a "
            "duplicate. Check the Sent folder.",
            level="error",
            lead_id=message["lead_id"],
            campaign_id=message["campaign_id"],
        )
    conn.commit()
    return len(stuck)
