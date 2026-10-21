"""Google Sheets -> leads sync.

The sheet is the source of truth for lead *data* (every column becomes a template
variable). Outreach Wise owns lead *status*. Rows removed from the sheet are kept.
"""

import json
import re
import sqlite3

from . import campaigns, db
from .config import Config
from .google import Google
from .security import decrypt

EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[a-z0-9-]{2,}$")
EMAIL_COLUMN_GUESSES = ("email", "email_address", "e_mail", "mail", "contact_email")


class SyncError(Exception):
    pass


def normalize_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(header).strip().lower()).strip("_")


def normalize_email(value: str) -> str | None:
    value = str(value).strip().lower().removeprefix("mailto:")
    return value if EMAIL.match(value) else None


def spreadsheet_id(url_or_id: str) -> str:
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url_or_id)
    return match.group(1) if match else url_or_id.strip()


def parse_rows(rows: list[list[str]], email_column: str = "email") -> tuple[list[dict], dict]:
    """Turn raw sheet values (header row first) into unique leads plus counters."""
    stats = {"rows": 0, "invalid": 0, "duplicates": 0}
    if not rows:
        return [], stats
    header = [normalize_key(h) for h in rows[0]]
    email_key = normalize_key(email_column)
    if email_key not in header:
        email_key = next((k for k in EMAIL_COLUMN_GUESSES if k in header), None)
        if not email_key:
            raise SyncError(
                f"No '{email_column}' column in the header row (found: {', '.join(filter(None, header))})"
            )

    leads, seen = [], set()
    for row in rows[1:]:
        if not any(str(cell).strip() for cell in row):
            continue
        stats["rows"] += 1
        fields = {
            key: str(value).strip()
            for key, value in zip(header, row, strict=False)
            if key and str(value).strip()
        }
        email = normalize_email(fields.get(email_key, ""))
        if not email:
            stats["invalid"] += 1
            continue
        if email in seen:
            stats["duplicates"] += 1
            continue
        seen.add(email)
        leads.append({"email": email, "fields": fields})
    return leads, stats


def upsert_leads(conn: sqlite3.Connection, leads: list[dict]) -> tuple[int, int]:
    """Insert new leads and refresh data of existing ones. Returns (created, updated)."""
    existing = {row["email"]: row["fields"] for row in conn.execute("SELECT email, fields FROM leads")}
    stamp = db.iso(db.now())
    new_rows, changed_rows = [], []
    for lead in leads:
        fields = json.dumps(lead["fields"], ensure_ascii=False, sort_keys=True)
        if lead["email"] not in existing:
            new_rows.append((lead["email"], fields, stamp, stamp))
        elif existing[lead["email"]] != fields:
            changed_rows.append((fields, stamp, lead["email"]))
    conn.executemany(
        "INSERT INTO leads (email, fields, status, created_at, updated_at) VALUES (?, ?, 'new', ?, ?)",
        new_rows,
    )
    conn.executemany("UPDATE leads SET fields = ?, updated_at = ? WHERE email = ?", changed_rows)
    return len(new_rows), len(changed_rows)


def sync(conn: sqlite3.Connection, cfg: Config, google: Google) -> dict:
    settings = db.get_settings(conn)
    if not settings["sheet_url"]:
        raise SyncError("No Google Sheet configured (Settings → Lead source).")
    account = conn.execute("SELECT * FROM accounts WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    if not account:
        raise SyncError("Connect a Google account first (Settings → Inboxes).")

    rows = google.sheet_values(
        decrypt(cfg.secret_key, account["refresh_token"]),
        spreadsheet_id(settings["sheet_url"]),
        settings["sheet_tab"] or "Sheet1",
    )
    leads, stats = parse_rows(rows, settings["sheet_email_column"] or "email")
    stats["created"], stats["updated"] = upsert_leads(conn, leads)
    stats["enrolled"] = campaigns.auto_enroll(conn)
    db.log_event(
        conn,
        "sync",
        f"Sheet sync: {stats['created']} new, {stats['updated']} updated, "
        f"{stats['duplicates']} duplicate and {stats['invalid']} invalid rows skipped"
        + (f", {stats['enrolled']} auto-enrolled" if stats["enrolled"] else ""),
    )
    db.set_kv(conn, "last_sync", {"at": db.iso(db.now()), **stats})
    conn.commit()
    return stats
