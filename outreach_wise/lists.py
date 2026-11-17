"""Lead lists and their sources.

A lead is unique by email and can be in several lists. A list is filled from a Google Sheet
tab (synced on a schedule, see sheets.py), from CSV or JSON uploads, or by adding leads one
by one. Every column becomes a template variable, as with the sheet.
"""

import csv
import io
import json
import sqlite3

from . import db

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class LeadImportError(Exception):
    pass


# --- lists ----------------------------------------------------------------------------------


def all_lists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ll.*,
            (SELECT COUNT(*) FROM lead_list_members m WHERE m.list_id = ll.id) AS total,
            (SELECT COUNT(*) FROM lead_list_members m JOIN leads l ON l.id = m.lead_id
             WHERE m.list_id = ll.id AND l.status = 'new') AS fresh
        FROM lead_lists ll ORDER BY ll.name COLLATE NOCASE
        """
    ).fetchall()


def get(conn: sqlite3.Connection, list_id: int | None) -> sqlite3.Row | None:
    if not list_id:
        return None
    return conn.execute("SELECT * FROM lead_lists WHERE id = ?", (list_id,)).fetchone()


def sheet_config(form) -> dict:
    """Sheet fields from a form; an empty URL means the list has no sheet."""
    return {
        "sheet_url": form.get("sheet_url", "").strip(),
        "sheet_tab": form.get("sheet_tab", "").strip() or "Sheet1",
        "sheet_email_column": form.get("sheet_email_column", "").strip() or "email",
        "sheet_columns": ", ".join(n.strip() for n in form.get("sheet_columns", "").split(",") if n.strip()),
    }


def create(conn: sqlite3.Connection, name: str, sheet: dict | None = None) -> tuple[int | None, str | None]:
    name = " ".join(name.split())
    if not name:
        return None, "Give the list a name."
    if conn.execute("SELECT 1 FROM lead_lists WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        return None, f"There is already a list called {name}."
    sheet = sheet or {}
    cur = conn.execute(
        "INSERT INTO lead_lists (name, sheet_url, sheet_tab, sheet_email_column, sheet_columns, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            name,
            sheet.get("sheet_url", ""),
            sheet.get("sheet_tab", "") if sheet.get("sheet_url") else "",
            sheet.get("sheet_email_column", "email") or "email",
            sheet.get("sheet_columns", ""),
            db.iso(db.now()),
        ),
    )
    return cur.lastrowid, None


def update(conn: sqlite3.Connection, list_id: int, name: str, sheet: dict) -> str | None:
    name = " ".join(name.split())
    if not name:
        return "Give the list a name."
    if conn.execute(
        "SELECT 1 FROM lead_lists WHERE name = ? COLLATE NOCASE AND id <> ?", (name, list_id)
    ).fetchone():
        return f"There is already a list called {name}."
    conn.execute(
        "UPDATE lead_lists SET name = ?, sheet_url = ?, sheet_tab = ?, sheet_email_column = ?, sheet_columns = ?"
        " WHERE id = ?",
        (
            name,
            sheet["sheet_url"],
            sheet["sheet_tab"] if sheet["sheet_url"] else "",
            sheet["sheet_email_column"],
            sheet["sheet_columns"],
            list_id,
        ),
    )
    return None


def delete(conn: sqlite3.Connection, list_id: int) -> None:
    """Delete the list only; its leads stay (they may be in other lists or campaigns)."""
    conn.execute("DELETE FROM lead_lists WHERE id = ?", (list_id,))


def add_members(conn: sqlite3.Connection, list_id: int, lead_ids: list[int]) -> int:
    stamp = db.iso(db.now())
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO lead_list_members (list_id, lead_id, added_at)"
        " SELECT ?, id, ? FROM leads WHERE id = ?",
        [(list_id, stamp, lead_id) for lead_id in lead_ids],
    )
    return conn.total_changes - before


def remove_members(conn: sqlite3.Connection, list_id: int, lead_ids: list[int]) -> int:
    before = conn.total_changes
    conn.executemany(
        "DELETE FROM lead_list_members WHERE list_id = ? AND lead_id = ?", [(list_id, i) for i in lead_ids]
    )
    return conn.total_changes - before


def lists_of(conn: sqlite3.Connection, lead_ids: list[int] | None = None) -> dict[int, list[int]]:
    """lead id -> the ids of the lists it is in."""
    out: dict[int, list[int]] = {}
    for lead_id, list_id in conn.execute("SELECT lead_id, list_id FROM lead_list_members ORDER BY list_id"):
        if lead_ids is None or lead_id in lead_ids:
            out.setdefault(lead_id, []).append(list_id)
    return out


def adopt_existing(conn: sqlite3.Connection) -> None:
    """Upgrade from the single sheet in Settings: that sheet becomes a list, and leads that are
    in no list join it (or a "Leads" list when there was no sheet). Safe to call any time."""
    settings = db.get_settings(conn)
    target = None
    if settings.get("sheet_url"):
        sheet = {
            "sheet_url": settings["sheet_url"],
            "sheet_tab": settings.get("sheet_tab") or "Sheet1",
            "sheet_email_column": settings.get("sheet_email_column") or "email",
            "sheet_columns": settings.get("sheet_columns", ""),
        }
        row = conn.execute(
            "SELECT id FROM lead_lists WHERE sheet_url = ? AND sheet_tab = ?",
            (sheet["sheet_url"], sheet["sheet_tab"]),
        ).fetchone()
        if row:
            target = row["id"]
        else:
            name = sheet["sheet_tab"] if sheet["sheet_tab"] != "Sheet1" else "Google Sheet"
            while conn.execute("SELECT 1 FROM lead_lists WHERE name = ?", (name,)).fetchone():
                name += " (sheet)"
            target, _ = create(conn, name, sheet)
            last = db.get_kv(conn, "last_sync")
            if last:
                conn.execute("UPDATE lead_lists SET last_sync = ? WHERE id = ?", (json.dumps(last), target))
        db.save_settings(conn, {"sheet_url": ""})  # the list owns the sheet now
    orphans = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM leads WHERE id NOT IN (SELECT lead_id FROM lead_list_members) ORDER BY id"
        )
    ]
    if orphans and target is None and not conn.execute("SELECT 1 FROM lead_lists").fetchone():
        target, _ = create(conn, "Leads")
    if orphans and target is not None:
        add_members(conn, target, orphans)


# --- importing ------------------------------------------------------------------------------


def upsert(conn: sqlite3.Connection, leads: list[dict], list_id: int | None = None) -> tuple[int, int]:
    """Insert new leads and merge new data into existing ones (new values win, other columns
    are kept, so a lead can carry columns from several sources). Adds them all to `list_id`.
    Returns (created, updated)."""
    existing = {row["email"]: row for row in conn.execute("SELECT id, email, fields FROM leads")}
    stamp = db.iso(db.now())
    created = updated = 0
    for lead in leads:
        row = existing.get(lead["email"])
        if row is None:
            conn.execute(
                "INSERT INTO leads (email, fields, status, created_at, updated_at) VALUES (?, ?, 'new', ?, ?)",
                (lead["email"], json.dumps(lead["fields"], ensure_ascii=False, sort_keys=True), stamp, stamp),
            )
            created += 1
            continue
        old = json.loads(row["fields"])
        merged = {**old, **lead["fields"]}
        if merged != old:
            conn.execute(
                "UPDATE leads SET fields = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False, sort_keys=True), stamp, row["id"]),
            )
            updated += 1
    if list_id and leads:
        emails = [lead["email"] for lead in leads]
        ids = []
        for start in range(0, len(emails), 500):
            chunk = emails[start : start + 500]
            ids += [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM leads WHERE email IN ({','.join('?' * len(chunk))})", chunk
                )
            ]
        add_members(conn, list_id, ids)
    return created, updated


def file_rows(filename: str, data: bytes) -> list[list[str]]:
    """A CSV (header row, any delimiter Excel or Sheets writes) or a JSON array of objects,
    as rows of cells with the header first, ready for sheets.parse_rows."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise LeadImportError("The file is larger than 10 MB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Older Turkish Excel installs export Windows-1254 CSVs. Decode those before
        # falling back to Latin-1, which would turn e.g. Ayşe into Ayþe. The marker
        # bytes are the Turkish letters that differ between the two encodings.
        turkish_bytes = {0xD0, 0xDD, 0xDE, 0xF0, 0xFD, 0xFE}
        encoding = "cp1254" if any(byte in turkish_bytes for byte in data) else "latin-1"
        text = data.decode(encoding)
    name = filename.lower()
    if name.endswith(".json") or text.lstrip().startswith(("[", "{")):
        try:
            items = json.loads(text)
        except ValueError as err:
            raise LeadImportError(f"The JSON file could not be read: {err}") from None
        if isinstance(items, dict):  # {"leads": [...]} or a single lead
            items = next((v for v in items.values() if isinstance(v, list)), [items])
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise LeadImportError(
                'The JSON file must be a list of objects, like [{"email": "…", "first_name": "…"}].'
            )
        header: list[str] = []
        for item in items:
            header += [k for k in item if k not in header]
        return [header] + [[_cell(item.get(k)) for k in header] for item in items]
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)

