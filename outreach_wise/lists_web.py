"""Web routes for lead lists and their sources (Leads → Lists).

  GET  /lists                   every list, with a form for a new one
  POST /lists                   create a list (optionally with a Google Sheet, synced right away)
  GET  /lists/<id>              one list: its sheet, file upload and add-a-lead forms
  POST /lists/<id>              rename it / change its sheet
  POST /lists/<id>/sync         sync its sheet now
  POST /lists/<id>/import       import a CSV or JSON file into it
  POST /lists/<id>/add          add one lead by hand
  POST /lists/<id>/delete       delete the list (its leads stay)
  POST /leads/lists             add the selected leads to a list, or remove them from one

Registered from web.create_app.
"""

import json
import sqlite3

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from . import db, lists, sheets
from .config import Config
from .google import Google, GoogleError


def _columns(conn: sqlite3.Connection, list_id: int) -> list[str]:
    """Column names used by the list's leads (for the add-a-lead form)."""
    seen: list[str] = []
    for (fields,) in conn.execute(
        "SELECT l.fields FROM leads l JOIN lead_list_members m ON m.lead_id = l.id"
        " WHERE m.list_id = ? ORDER BY m.added_at DESC LIMIT 200",
        (list_id,),
    ):
        seen += [k for k in json.loads(fields) if k not in seen and k != "email"]
    return seen[:12]


def register(app: Flask, cfg: Config, google: Google) -> None:
    def conn() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect(cfg.database_path)
        return g.db

    def load(list_id: int) -> sqlite3.Row:
        return lists.get(conn(), list_id) or abort(404)

    def to_list(list_id: int, anchor: str = ""):
        return redirect(url_for("lead_list", list_id=list_id) + anchor)

    def sync_list(c: sqlite3.Connection, list_id: int) -> None:
        try:
            stats = sheets.sync(c, cfg, google, list_id)
            flash(
                f"Synced: {stats['created']} new, {stats['updated']} updated, {stats['duplicates']} duplicates and "
                f"{stats['invalid']} invalid rows skipped, {stats['no_email']} rows without an email.",
                "success",
            )
        except (sheets.SyncError, GoogleError, ValueError) as err:
            c.rollback()
            flash(f"Sync failed: {err}", "error")

    @app.get("/lists")
    def lead_lists():
        return render_template("lists.html", rows=lists.all_lists(conn()))

    @app.post("/lists")
    def lead_list_create():
        c = conn()
        sheet = lists.sheet_config(request.form) if request.form.get("source") == "sheet" else None
        if sheet is not None and not sheet["sheet_url"]:
            flash("Paste the Google Sheet's URL, or pick another source.", "error")
            return redirect(url_for("lead_lists"))
        list_id, error = lists.create(c, request.form.get("name", ""), sheet)
        if error:
            flash(error, "error")
            return redirect(url_for("lead_lists"))
        c.commit()
        if sheet:
            sync_list(c, list_id)
        else:
            flash("List created. Add leads from a file or one by one.", "success")
        return to_list(list_id)

    @app.get("/lists/<int:list_id>")
    def lead_list(list_id: int):
        c = conn()
        row = load(list_id)
        counts = dict(
            c.execute(
                "SELECT l.status, COUNT(*) FROM leads l JOIN lead_list_members m ON m.lead_id = l.id"
                " WHERE m.list_id = ? GROUP BY l.status",
                (list_id,),
            ).fetchall()
        )
        return render_template(
            "list.html",
            row=row,
            counts=counts,
            total=sum(counts.values()),
            last=json.loads(row["last_sync"]) if row["last_sync"] else None,
            columns=_columns(c, list_id),
            campaigns_using=c.execute(
                "SELECT id, name, auto_enroll FROM campaigns WHERE list_id = ? ORDER BY id DESC", (list_id,)
            ).fetchall(),
        )

    @app.post("/lists/<int:list_id>")
    def lead_list_save(list_id: int):
        c = conn()
        load(list_id)
        error = lists.update(c, list_id, request.form.get("name", ""), lists.sheet_config(request.form))
        if error:
            flash(error, "error")
        else:
            c.commit()
            flash("List saved.", "success")
        return to_list(list_id, "#settings")

    @app.post("/lists/<int:list_id>/sync")
    def lead_list_sync(list_id: int):
        c = conn()
        if not load(list_id)["sheet_url"]:
            flash("This list has no Google Sheet.", "error")
        else:
            sync_list(c, list_id)
        return to_list(list_id)

    @app.post("/lists/<int:list_id>/import")
    def lead_list_import(list_id: int):
        c = conn()
        load(list_id)
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV or JSON file.", "error")
            return to_list(list_id, "#import")
        try:
            rows = lists.file_rows(upload.filename, upload.read(lists.MAX_UPLOAD_BYTES + 1))
            stats = sheets.import_rows(
                c,
                list_id,
                rows,
                f"File {upload.filename}",
                request.form.get("email_column", "").strip() or "email",
            )
        except (lists.LeadImportError, sheets.SyncError) as err:
            c.rollback()
            flash(f"Not imported: {err}", "error")
            return to_list(list_id, "#import")
        c.commit()
        flash(
            f"Imported {upload.filename}: {stats['created']} new, {stats['updated']} updated,"
            f" {stats['duplicates']} duplicates and {stats['invalid']} invalid rows skipped,"
            f" {stats['no_email']} rows without an email.",
            "success",
        )
        return to_list(list_id)

    @app.post("/lists/<int:list_id>/add")
    def lead_list_add(list_id: int):
        c = conn()
        load(list_id)
        form = request.form
        email = sheets.normalize_email(form.get("email", ""))
        if not email:
            flash("Enter a valid email address.", "error")
            return to_list(list_id, "#add")
        fields = {"email": email}
        names = form.getlist("field_name")
        values = form.getlist("field_value")
        for name, value in zip(names, values, strict=False):
            key = sheets.normalize_key(name)
            if key and key != "email" and value.strip():
                fields[key] = value.strip()
        existed = c.execute("SELECT id FROM leads WHERE email = ?", (email,)).fetchone()
        lists.upsert(c, [{"email": email, "fields": fields}], list_id)
        lead_id = c.execute("SELECT id FROM leads WHERE email = ?", (email,)).fetchone()["id"]
        db.log_event(c, "leads", f"Added {email} by hand", lead_id=lead_id)
        c.commit()
        flash(
            f"{email} was already a lead: its data was updated and it's now in this list."
            if existed
            else f"Added {email}.",
            "success",
        )
        return to_list(list_id, "#add")

    @app.post("/lists/<int:list_id>/delete")
    def lead_list_delete(list_id: int):
        c = conn()
        row = load(list_id)
        lists.delete(c, list_id)
        db.log_event(c, "leads", f"Deleted the list {row['name']} (its leads were kept)")
        c.commit()
        flash(f"Deleted the list {row['name']}. Its leads are still in Leads.", "success")
        return redirect(url_for("lead_lists"))

    @app.post("/leads/lists")
    def leads_to_list():
        c = conn()
        ids = sorted({int(v) for v in request.form.getlist("lead_id") if v.isdigit()})
        list_id = int(request.form.get("list_id") or 0)
        row = lists.get(c, list_id)
        back = request.form.get("next", "")
        back = back if back.startswith("/leads") else url_for("leads")
        if not row or not ids:
            flash("Pick a list and at least one lead.", "error")
            return redirect(back)
        if request.form.get("action") == "remove":
            n = lists.remove_members(c, list_id, ids)
            flash(f"Removed {n} lead{'s' if n != 1 else ''} from {row['name']}.", "success")
        else:
            n = lists.add_members(c, list_id, ids)
            flash(f"Added {n} lead{'s' if n != 1 else ''} to {row['name']}.", "success")
        c.commit()
        return redirect(back)
