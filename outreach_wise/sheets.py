"""Google Sheets -> leads sync.

The sheet is the source of truth for lead *data* (every column becomes a template
variable). Outreach Wise owns lead *status*. Rows removed from the sheet are kept.
"""

import json
import re
import sqlite3

from . import campaigns, db, lists
from .config import Config
from .google import Google, GoogleError
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


def column_letter(index: int) -> str:
    """0 -> "a", 25 -> "z", 26 -> "aa": the sheet's own column letters."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(97 + rest) + letters
    return letters


def has_header(first_row: list[str]) -> bool:
    """A first row that contains an email address is data, not a header."""
    return not any(normalize_email(cell) for cell in first_row)


def parse_rows(
    rows: list[list[str]], email_column: str = "email", column_names: str = ""
) -> tuple[list[dict], dict]:
    """Turn raw sheet values into unique leads plus counters.

    With a header row, every header becomes a variable name. Without one (detected
    automatically), columns are named by `column_names` ("email, first_name, ...") or
    after their letters ({{column_b}}), and the email column is found by its contents.
    """
    stats = {"rows": 0, "no_email": 0, "invalid": 0, "duplicates": 0, "header": True}
    if not rows:
        return [], stats
    if has_header(rows[0]):
        header, data = [normalize_key(h) for h in rows[0]], rows[1:]
        email_key = normalize_key(email_column)
        if email_key not in header:
            email_key = next((k for k in EMAIL_COLUMN_GUESSES if k in header), None)
            if not email_key:
                raise SyncError(
                    f"No '{email_column}' column in the header row (found: {', '.join(filter(None, header))})"
                )
    else:
        stats["header"] = False
        data = rows
        width = max(len(row) for row in rows)
        names = [normalize_key(name) for name in column_names.split(",")] if column_names.strip() else []
        header = [
            names[i] if i < len(names) and names[i] else f"column_{column_letter(i)}" for i in range(width)
        ]
        sample = rows[:200]
        email_index = max(
            range(width),
            key=lambda i: sum(1 for row in sample if i < len(row) and normalize_email(row[i])),
        )
        if header[email_index].startswith("column_"):
            header[email_index] = "email"
        email_key = header[email_index]

    leads, seen = [], set()
    for row in data:
        if not any(str(cell).strip() for cell in row):
            continue
        stats["rows"] += 1
        fields = {
            key: str(value).strip()
            for key, value in zip(header, row, strict=False)
            if key and str(value).strip()
        }
        if not fields.get(email_key):
            stats["no_email"] += 1  # e.g. a lead whose email isn't found yet: not an error
            continue
        email = normalize_email(fields[email_key])
        if not email:
            stats["invalid"] += 1
            continue
        if email in seen:
            stats["duplicates"] += 1
            continue
        seen.add(email)
        leads.append({"email": email, "fields": fields})
    return leads, stats


def upsert_leads(conn: sqlite3.Connection, leads: list[dict], list_id: int | None = None) -> tuple[int, int]:
    """Insert new leads and merge new data into existing ones. Returns (created, updated)."""
    return lists.upsert(conn, leads, list_id)


def import_rows(
    conn: sqlite3.Connection,
    list_id: int,
    rows: list[list[str]],
    source: str,
    email_column: str = "email",
    column_names: str = "",
) -> dict:
    """Rows (header first, or none) from a sheet or a file → leads in a list. Records the run
    on the list and in the activity log, then auto-enrolls. Returns the counters."""
    leads, stats = parse_rows(rows, email_column or "email", column_names)
    stats["created"], stats["updated"] = lists.upsert(conn, leads, list_id)
    stats["enrolled"] = campaigns.auto_enroll(conn)
    stats.update(at=db.iso(db.now()), source=source)
    name = conn.execute("SELECT name FROM lead_lists WHERE id = ?", (list_id,)).fetchone()["name"]
    conn.execute("UPDATE lead_lists SET last_sync = ? WHERE id = ?", (json.dumps(stats), list_id))
    db.log_event(
        conn,
        "sync",
        f"{source} → {name}: {stats['created']} new, {stats['updated']} updated, "
        f"{stats['duplicates']} duplicate and {stats['invalid']} invalid rows skipped, "
        f"{stats['no_email']} rows without an email"
        + (f", {stats['enrolled']} auto-enrolled" if stats["enrolled"] else ""),
    )
    return stats


def sync(conn: sqlite3.Connection, cfg: Config, google: Google, list_id: int | None = None) -> dict:
    """Sync every list that has a Google Sheet (or just `list_id`). Returns the summed counters."""
    lists.adopt_existing(conn)
    sql = (
        "SELECT * FROM lead_lists WHERE sheet_url <> ''" + (" AND id = ?" if list_id else "") + " ORDER BY id"
    )
    sheet_lists = conn.execute(sql, (list_id,) if list_id else ()).fetchall()
    if not sheet_lists:
        raise SyncError("No list has a Google Sheet yet (Leads → Lists).")
    account = conn.execute(
        "SELECT * FROM accounts WHERE enabled = 1 AND transport = 'gmail_api' ORDER BY id LIMIT 1"
    ).fetchone()
    if not account:
        raise SyncError("Connect a Google account first (Settings → Inboxes).")

    keys = ("rows", "created", "updated", "duplicates", "invalid", "no_email", "enrolled")
    total = dict.fromkeys(keys, 0)
    errors = []
    for sheet_list in sheet_lists:
        try:
            rows = google.sheet_values(
                decrypt(cfg.secret_key, account["refresh_token"]),
                spreadsheet_id(sheet_list["sheet_url"]),
                sheet_list["sheet_tab"] or "Sheet1",
            )
            stats = import_rows(
                conn,
                sheet_list["id"],
                rows,
                "Sheet sync",
                sheet_list["sheet_email_column"],
                sheet_list["sheet_columns"],
            )
        except (SyncError, GoogleError, ValueError) as err:
            errors.append(f"{sheet_list['name']}: {err}")
            failed = {"at": db.iso(db.now()), "source": "Sheet sync", "error": str(err)}
            conn.execute(
                "UPDATE lead_lists SET last_sync = ? WHERE id = ?", (json.dumps(failed), sheet_list["id"])
            )
            continue
        for key in keys:
            total[key] += stats.get(key, 0)
        total["header"] = stats["header"]
    if errors and len(errors) == len(sheet_lists):
        raise SyncError("; ".join(errors))
    for message in errors:
        db.log_event(conn, "sync", f"Sheet sync failed for {message}", level="error")
    db.set_kv(conn, "last_sync", {"at": db.iso(db.now()), **total})
    conn.commit()
    return total
