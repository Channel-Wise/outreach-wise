"""Web routes for AI personalization: settings, playbooks, review queue, campaign settings.

Registered on the app by web.create_app(); kept apart so the feature stays self-contained.
"""

import json
import sqlite3
from datetime import timedelta

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import campaigns, db
from ..config import Config
from ..rendering import MissingVariable, render, single_line
from . import checks, generate, keys, playbooks, providers, scheduler
from .playbooks import SOURCES, Checks, Field, Playbook, PlaybookError

STATUSES = ("needs_review", "approved", "skipped", "failed", "pending")
STATUS_LABELS = {
    "needs_review": "Waiting",
    "approved": "Approved",
    "skipped": "Skipped",
    "failed": "Failed",
    "pending": "Researching",
}
BLANK = """+++
name = "New playbook"
language = "auto"

[input]
column = "channel_url"
sources = ["youtube_channel"]

[fields.hook]
describe = "1-3 short sentences about one concrete, verifiable thing about this lead."
max_words = 60

[checks]
banned = []
one_link = true
numbers_in_data = true
+++

# Who you are writing as

# Rules
- Use only facts from the research data. If unsure, leave it out and say so in notes_for_reviewer.
"""


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def playbook_from_form(form) -> str:
    """The editor form → .playbook.md text (raw mode: the text as typed). Raises PlaybookError."""
    if form.get("mode") == "raw":
        return form.get("raw", "").replace("\r\n", "\n")
    fields = []
    rows = zip(
        form.getlist("f_name"),
        form.getlist("f_describe"),
        form.getlist("f_max_words"),
        form.getlist("f_fallback"),
        form.getlist("f_alternatives"),
        strict=False,
    )
    for name, describe, max_words, fallback, alternatives in rows:
        name = name.strip().lower().removeprefix("ai_")
        if not name and not describe.strip():
            continue
        fallback = fallback.strip()
        fields.append(
            Field(
                name=name,
                describe=" ".join(describe.split()),
                max_words=_int(max_words),
                required=not fallback,
                fallback=fallback or None,
                alternatives=_int(alternatives, 0) or 0,
            )
        )

    def pair(low, high):
        low, high = _int(form.get(low)), _int(form.get(high))
        return (low, high) if low is not None and high is not None else None

    playbook = Playbook(
        name=" ".join(form.get("name", "").split()),
        language=form.get("language", "auto").strip() or "auto",
        input_column=form.get("input_column", "").strip().lower(),
        sources=[s for s in form.getlist("sources") if s in SOURCES],
        fields=fields,
        instructions=form.get("instructions", "").replace("\r\n", "\n"),
        checks=Checks(
            email_words=pair("email_min", "email_max"),
            subject_words=pair("subject_min", "subject_max"),
            banned=[b for b in form.get("banned", "").replace("\r\n", "\n").split("\n") if b.strip()],
            one_link=form.get("one_link") == "on",
            numbers_in_data=form.get("numbers_in_data") == "on",
        ),
    )
    text = playbooks.dump(playbook)
    playbooks.parse(text)
    return text


def register(app: Flask, cfg: Config) -> None:
    def conn() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect(cfg.database_path)
        return g.db

    def lead_columns(c) -> list[str]:
        keys_seen: set[str] = set()
        for (fields,) in c.execute("SELECT fields FROM leads ORDER BY id DESC LIMIT 200"):
            keys_seen.update(json.loads(fields))
        return sorted(keys_seen)

    @app.context_processor
    def ai_globals():
        from flask import session  # noqa: PLC0415

        if not session.get("auth"):
            return {}
        c = conn()
        waiting = 0
        if db.get_settings(c)["ai_enabled"]:
            waiting = c.execute("SELECT COUNT(*) FROM ai_fields WHERE status = 'needs_review'").fetchone()[0]
        return {"ai_waiting": waiting, "ai_campaign": ai_campaign, "ai_settings": ai_settings}

    # --- context for existing pages ---------------------------------------------------------

    def ai_settings() -> dict:
        c = conn()
        return {
            "models": providers.MODELS,
            "efforts": providers.EFFORTS,
            "has_sdk": providers.anthropic_installed(),
            "has_cli": bool(providers.claude_cli()),
            "api_key": keys.masked(c, cfg, "ai_api_key"),
            "api_key_env": keys.from_env(cfg, "ai_api_key"),
            "youtube_key": keys.masked(c, cfg, "youtube_api_key"),
            "youtube_key_env": keys.from_env(cfg, "youtube_api_key"),
            "usage": generate.usage_today(c),
            "paused": db.get_kv(c, "ai_paused"),
        }

    def ai_campaign(campaign) -> dict:
        c = conn()
        settings = db.get_settings(c)
        playbook = playbooks.load(c, campaign["playbook_id"])
        counts = {s: 0 for s in STATUSES}
        if campaign["playbook_id"]:
            for row in c.execute(
                "SELECT a.status, COUNT(*) AS n FROM ai_fields a WHERE a.playbook_id = ? AND a.lead_id IN"
                " (SELECT lead_id FROM enrollments WHERE campaign_id = ?) GROUP BY a.status",
                (campaign["playbook_id"], campaign["id"]),
            ):
                key = "pending" if row["status"] == "researching" else row["status"]
                counts[key] = counts.get(key, 0) + row["n"]
        return {
            "enabled": settings["ai_enabled"],
            "playbooks": [p for p in playbooks.all_playbooks(c) if p["parsed"]],
            "playbook": playbook,
            "variables": playbook.variables if playbook else [],
            "counts": counts,
            "total": sum(counts.values()),
        }

    # --- settings ---------------------------------------------------------------------------

    @app.post("/settings/ai")
    def ai_settings_save():
        c = conn()
        form = request.form
        try:
            budget = float(form.get("ai_monthly_budget_usd", "20"))
            if not 0 <= budget <= 10000:
                raise ValueError
        except ValueError:
            flash("Not saved: the monthly budget must be a number.", "error")
            return redirect(url_for("settings_page") + "#ai")
        provider = form.get("ai_provider", "anthropic")
        updates = {
            "ai_enabled": form.get("ai_enabled") == "on",
            "ai_provider": provider if provider in ("anthropic", "claude_code") else "anthropic",
            "ai_model": form.get("ai_model")
            if form.get("ai_model") in providers.MODELS
            else providers.MODELS[0],
            "ai_effort": form.get("ai_effort") if form.get("ai_effort") in providers.EFFORTS else "medium",
            "ai_daily_limit": max(1, min(_int(form.get("ai_daily_limit"), 300), 5000)),
            "ai_monthly_budget_usd": budget,
            "ai_require_approval": form.get("ai_require_approval") == "on",
        }
        db.save_settings(c, updates)
        for name in ("ai_api_key", "youtube_api_key"):
            value = form.get(name, "")
            if form.get(f"clear_{name}") == "on":
                keys.set_key(c, cfg, name, "")
            elif value.strip():
                keys.set_key(c, cfg, name, value)
        db.log_event(c, "settings", "Updated AI personalization settings")
        c.commit()
        if form.get("action") == "test":
            try:
                result = providers.for_settings(c, cfg, db.get_settings(c)).generate(
                    "Answer with JSON.",
                    'Reply with {"ok": true}.',
                    {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                    max_tokens=2000,
                )
                generate.record_spend(c, result.cost_usd)
                c.commit()
                flash(f"The model answered ({result.model}). AI settings saved.", "success")
            except providers.ProviderError as err:
                flash(f"Settings saved, but the test failed: {err}", "error")
        else:
            flash("AI settings saved.", "success")
        return redirect(url_for("settings_page") + "#ai")

    # --- playbooks --------------------------------------------------------------------------

    def require_ai():
        if not db.get_settings(conn())["ai_enabled"]:
            flash("Turn on AI personalization in Settings first.", "info")
            return redirect(url_for("settings_page") + "#ai")
        return None

    @app.get("/playbooks")
    def playbook_list():
        if (gate := require_ai()) is not None:
            return gate
        return render_template("playbooks.html", playbooks=playbooks.all_playbooks(conn()), sources=SOURCES)

    def editor(source: str, row=None, error: str | None = None, status: int = 200):
        c = conn()
        try:
            parsed = playbooks.parse(source)
        except PlaybookError:
            parsed = None
        columns = lead_columns(c)
        input_column = parsed.input_column if parsed else ""
        test_leads = [
            r
            for r in c.execute("SELECT id, email, fields FROM leads ORDER BY id DESC LIMIT 400")
            if not input_column or str(json.loads(r["fields"]).get(input_column, "")).strip()
        ][:60]
        examples = (
            c.execute("SELECT COUNT(*) FROM ai_examples WHERE playbook_id = ?", (row["id"],)).fetchone()[0]
            if row
            else 0
        )
        return (
            render_template(
                "playbook.html",
                row=row,
                pb=parsed,
                source=source,
                error=error,
                columns=columns,
                sources=SOURCES,
                test_leads=test_leads,
                examples=examples,
            ),
            status,
        )

    @app.get("/playbooks/new")
    def playbook_new():
        if (gate := require_ai()) is not None:
            return gate
        c = conn()
        return render_template(
            "playbook_new.html",
            examples=[p for p in playbooks.all_playbooks(c) if p["builtin"]],
            columns=lead_columns(c),
        )

    @app.post("/playbooks/new")
    def playbook_create():
        c = conn()
        start = request.form.get("start")
        if start == "example":
            row = playbooks.get(c, _int(request.form.get("example_id")))
            if not row:
                abort(404)
            return editor(
                row["source"].replace(f'name = "{row["name"]}"', f'name = "{row["name"]} (copy)"', 1)
            )
        if start == "guide":
            upload = request.files.get("guide_file")
            pdf, text = None, request.form.get("guide_text", "")
            if upload and upload.filename:
                data = upload.read()
                if upload.filename.lower().endswith(".pdf"):
                    pdf = data
                else:
                    text = data.decode("utf-8", errors="replace")
            if not pdf and not text.strip():
                flash("Upload or paste your outreach guide.", "error")
                return redirect(url_for("playbook_new"))
            try:
                source = generate.draft_playbook(
                    c, cfg, db.get_settings(c), guide_text=text, pdf=pdf, columns=lead_columns(c)
                )
            except (providers.ProviderError, PlaybookError) as err:
                flash(f"Couldn't draft a playbook: {err}", "error")
                return redirect(url_for("playbook_new"))
            name = request.form.get("name", "").strip()
            if name:
                parsed = playbooks.parse(source)
                parsed.name = name[:120]
                source = playbooks.dump(parsed)
            flash("Here's the draft. Check it, test it on a lead, then save.", "info")
            return editor(source)
        return editor(BLANK)

    @app.get("/playbooks/<int:playbook_id>")
    def playbook_edit(playbook_id: int):
        if (gate := require_ai()) is not None:
            return gate
        row = playbooks.get(conn(), playbook_id) or abort(404)
        return editor(row["source"], row)

    @app.post("/playbooks/save")
    def playbook_save():
        c = conn()
        playbook_id = _int(request.form.get("playbook_id"))
        row = playbooks.get(c, playbook_id) if playbook_id else None
        try:
            source = playbook_from_form(request.form)
        except PlaybookError as err:
            return editor(request.form.get("raw") or BLANK, row, str(err), 400)
        saved, error = playbooks.save(c, source, row["id"] if row and not row["builtin"] else None)
        if error:
            return editor(source, row, error, 400)
        db.log_event(c, "settings", f"Saved playbook {playbooks.parse(source).name}")
        c.commit()
        flash("Playbook saved.", "success")
        return redirect(url_for("playbook_edit", playbook_id=saved))

    @app.post("/playbooks/test")
    def playbook_test():
        c = conn()
        try:
            source = playbook_from_form(request.form)
            playbook = playbooks.parse(source)
        except PlaybookError as err:
            return jsonify(status="error", error=str(err)), 400
        lead = c.execute("SELECT * FROM leads WHERE id = ?", (_int(request.form.get("lead_id")),)).fetchone()
        if not lead:
            return jsonify(status="error", error="Pick a lead to test with."), 400
        outcome = generate.write(
            c, cfg, db.get_settings(c), playbook, lead, playbook_id=_int(request.form.get("playbook_id"))
        )
        c.commit()
        return jsonify(outcome)

    @app.get("/playbooks/<int:playbook_id>/export")
    def playbook_export(playbook_id: int):
        row = playbooks.get(conn(), playbook_id) or abort(404)
        slug = "".join(ch if ch.isalnum() else "-" for ch in row["name"].lower()).strip("-") or "playbook"
        return Response(
            row["source"],
            mimetype="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{slug}.playbook.md"'},
        )

    @app.post("/playbooks/import")
    def playbook_import():
        c = conn()
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a .playbook.md file.", "error")
            return redirect(url_for("playbook_list"))
        saved, error = playbooks.save(c, upload.read().decode("utf-8", errors="replace"))
        if error:
            flash(f"Not imported: {error}", "error")
            return redirect(url_for("playbook_list"))
        c.commit()
        flash("Playbook imported.", "success")
        return redirect(url_for("playbook_edit", playbook_id=saved))

    @app.post("/playbooks/<int:playbook_id>/delete")
    def playbook_delete(playbook_id: int):
        c = conn()
        error = playbooks.delete(c, playbook_id)
        if error:
            flash(error, "error")
            return redirect(url_for("playbook_edit", playbook_id=playbook_id))
        c.commit()
        flash("Playbook deleted.", "success")
        return redirect(url_for("playbook_list"))

    # --- campaign ---------------------------------------------------------------------------

    @app.post("/campaigns/<int:campaign_id>/ai")
    def campaign_ai(campaign_id: int):
        c = conn()
        if not campaigns.get(c, campaign_id):
            abort(404)
        playbook_id = _int(request.form.get("playbook_id"))
        if playbook_id and not playbooks.load(c, playbook_id):
            flash("That playbook has an error; fix it first.", "error")
            return redirect(url_for("campaign", campaign_id=campaign_id) + "#settings")
        c.execute(
            "UPDATE campaigns SET playbook_id = ?, require_approval = ? WHERE id = ?",
            (playbook_id or None, int(request.form.get("require_approval") == "on"), campaign_id),
        )
        # Re-plan with the new playbook.
        c.execute(
            "UPDATE enrollments SET pinned_variant_id = NULL, pinned_step = NULL WHERE campaign_id = ?"
            " AND status = 'active'",
            (campaign_id,),
        )
        c.commit()
        flash("AI personalization settings saved.", "success")
        return redirect(url_for("campaign", campaign_id=campaign_id) + "#settings")

    # --- review -----------------------------------------------------------------------------

    def preview_email(c, row, playbook, vals: dict) -> dict | None:
        """Render the email this lead will get, with these field values."""
        enrollment = c.execute(
            """
            SELECT e.* FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
            WHERE e.lead_id = ? AND c.playbook_id = ?
            ORDER BY e.status = 'active' DESC, e.next_send_at LIMIT 1
            """,
            (row["lead_id"], row["playbook_id"]),
        ).fetchone()
        if not enrollment:
            return None
        step = c.execute(
            "SELECT * FROM steps WHERE campaign_id = ? AND position = ?",
            (enrollment["campaign_id"], enrollment["next_step"]),
        ).fetchone()
        variant = None
        if step and enrollment["pinned_step"] == step["position"] and enrollment["pinned_variant_id"]:
            variant = c.execute(
                "SELECT * FROM variants WHERE id = ?", (enrollment["pinned_variant_id"],)
            ).fetchone()
        if not variant and step:
            variant = c.execute(
                "SELECT * FROM variants WHERE step_id = ? AND active = 1 ORDER BY label", (step["id"],)
            ).fetchone()
        if not variant:
            return None
        lead = c.execute("SELECT * FROM leads WHERE id = ?", (row["lead_id"],)).fetchone()
        variables = campaigns.lead_variables(cfg, lead)
        variables.update({f"ai_{k}": v for k, v in vals.items()})
        try:
            subject = single_line(render(variant["subject"], variables)) if variant["subject"].strip() else ""
            body = render(variant["body"], variables)
        except MissingVariable as err:
            return {"error": str(err), "campaign": campaigns.get(c, enrollment["campaign_id"])["name"]}
        campaign = campaigns.get(c, enrollment["campaign_id"])
        return {
            "campaign": campaign["name"],
            "campaign_id": campaign["id"],
            "step": step["position"],
            "label": variant["label"],
            "subject": subject or "(reply in the same thread)",
            "body": body,
            "subject_template": variant["subject"],
            "body_template": variant["body"],
            "checks": checks.email_checks(playbook, subject, body),
        }

    def already_sent(c, row, vals: dict) -> list[dict]:
        found = []
        for message in c.execute(
            "SELECT body, sent_at, subject FROM messages WHERE lead_id = ? AND mode = 'live' AND status = 'sent'",
            (row["lead_id"],),
        ):
            for name, value in vals.items():
                if len(value) > 24 and value in message["body"]:
                    found.append({"field": name, "sent_at": message["sent_at"]})
        return found

    @app.get("/review")
    def review():
        if (gate := require_ai()) is not None:
            return gate
        c = conn()
        settings = db.get_settings(c)
        status = request.args.get("status", "needs_review")
        if status not in STATUSES:
            status = "needs_review"
        campaign_id = _int(request.args.get("campaign"))
        where, params = "a.status = ?", [status if status != "pending" else "pending"]
        if status == "pending":
            where = "a.status IN ('pending', 'researching')"
            params = []
        if campaign_id:
            where += " AND a.playbook_id = (SELECT playbook_id FROM campaigns WHERE id = ?)"
            where += " AND a.lead_id IN (SELECT lead_id FROM enrollments WHERE campaign_id = ?)"
            params += [campaign_id, campaign_id]
        items = c.execute(
            f"SELECT a.*, l.email, l.fields AS lead_fields, p.name AS playbook_name FROM ai_fields a"
            f" JOIN leads l ON l.id = a.lead_id JOIN playbooks p ON p.id = a.playbook_id"
            f" WHERE {where} ORDER BY a.generated_at IS NULL, a.generated_at, a.id LIMIT 300",
            params,
        ).fetchall()
        counts = {}
        for s in STATUSES:
            sql = "a.status IN ('pending', 'researching')" if s == "pending" else "a.status = ?"
            args = [] if s == "pending" else [s]
            if campaign_id:
                sql += " AND a.playbook_id = (SELECT playbook_id FROM campaigns WHERE id = ?)"
                sql += " AND a.lead_id IN (SELECT lead_id FROM enrollments WHERE campaign_id = ?)"
                args += [campaign_id, campaign_id]
            counts[s] = c.execute(f"SELECT COUNT(*) FROM ai_fields a WHERE {sql}", args).fetchone()[0]

        selected = None
        selected_id = _int(request.args.get("id")) or (items[0]["id"] if items else None)
        current = next((i for i in items if i["id"] == selected_id), None) or (
            c.execute(
                "SELECT a.*, l.email, l.fields AS lead_fields, p.name AS playbook_name FROM ai_fields a"
                " JOIN leads l ON l.id = a.lead_id JOIN playbooks p ON p.id = a.playbook_id WHERE a.id = ?",
                (selected_id,),
            ).fetchone()
            if selected_id
            else None
        )
        if current:
            playbook = playbooks.load(c, current["playbook_id"])
            vals = json.loads(current["vals"] or "{}")
            stale = bool(
                current["generated_at"]
                and db.parse(current["generated_at"])
                < db.now() - timedelta(days=int(settings["ai_stale_days"]))
            )
            selected = {
                "row": current,
                "playbook": playbook,
                "vals": vals,
                "alternatives": json.loads(current["alternatives"] or "{}"),
                "evidence": json.loads(current["evidence"] or "[]"),
                "checks": json.loads(current["checks"] or "[]"),
                "lead": json.loads(current["lead_fields"] or "{}"),
                "email": preview_email(c, current, playbook, vals) if playbook and vals else None,
                "sent_before": already_sent(c, current, vals),
                "stale": stale,
                "position": next((n for n, i in enumerate(items, 1) if i["id"] == current["id"]), None),
            }
        clean = sum(
            1
            for i in items
            if i["status"] == "needs_review" and all(ch.get("ok") for ch in json.loads(i["checks"] or "[]"))
        )
        return render_template(
            "review.html",
            items=items,
            selected=selected,
            status=status,
            labels=STATUS_LABELS,
            counts=counts,
            campaign_id=campaign_id,
            campaign_list=c.execute(
                "SELECT id, name FROM campaigns WHERE playbook_id IS NOT NULL ORDER BY id DESC"
            ).fetchall(),
            clean=clean,
        )

    def review_redirect(after_id: int | None = None):
        args = {k: v for k, v in request.form.items() if k in ("status", "campaign") and v}
        if after_id:
            args["id"] = after_id
        return redirect(url_for("review", **args))

    def next_waiting(c, current_id: int) -> int | None:
        row = c.execute(
            "SELECT id FROM ai_fields WHERE status = 'needs_review' AND id != ? ORDER BY generated_at, id LIMIT 1",
            (current_id,),
        ).fetchone()
        return row["id"] if row else None

    def approve(c, row, new_vals: dict | None = None) -> None:
        old = json.loads(row["vals"] or "{}")
        new_vals = new_vals if new_vals is not None else old
        stamp = db.iso(db.now())
        changed = {k: v for k, v in new_vals.items() if v != old.get(k, "")}
        for name, value in changed.items():
            if old.get(name):
                c.execute(
                    "INSERT INTO ai_examples (playbook_id, field, before, after, created_at) VALUES (?, ?, ?, ?, ?)",
                    (row["playbook_id"], name, old[name], value, stamp),
                )
        c.execute(
            "UPDATE ai_fields SET status = 'approved', vals = ?, original_vals = COALESCE(original_vals, ?),"
            " reviewed_at = ? WHERE id = ?",
            (
                json.dumps(new_vals, ensure_ascii=False),
                json.dumps(old, ensure_ascii=False) if changed else None,
                stamp,
                row["id"],
            ),
        )
        scheduler.release_held(c, row["lead_id"], row["playbook_id"])

    def load_row(ai_id: int):
        row = conn().execute("SELECT * FROM ai_fields WHERE id = ?", (ai_id,)).fetchone()
        return row or abort(404)

    @app.post("/review/<int:ai_id>/approve")
    def review_approve(ai_id: int):
        c = conn()
        row = load_row(ai_id)
        playbook = playbooks.load(c, row["playbook_id"])
        if not playbook:
            abort(400)
        vals = {
            f.name: " ".join(request.form.get(f"val_{f.name}", "").split(" ")).strip()
            for f in playbook.fields
        }
        missing = [f.name for f in playbook.fields if f.required and not vals[f.name]]
        if missing:
            flash(f"ai_{missing[0]} can't be empty. Skip the lead instead.", "error")
            return review_redirect(ai_id)
        approve(c, row, vals)
        c.commit()
        flash("Approved.", "success")
        return review_redirect(next_waiting(c, ai_id))

    @app.post("/review/<int:ai_id>/regenerate")
    def review_regenerate(ai_id: int):
        c = conn()
        load_row(ai_id)
        c.execute(
            "UPDATE ai_fields SET status = 'pending', attempts = 0, error = NULL, regenerate_note = ? WHERE id = ?",
            (request.form.get("note", "").strip()[:1000] or None, ai_id),
        )
        c.commit()
        flash("Queued for a new draft. It shows up here again in a minute or two.", "info")
        return review_redirect(next_waiting(c, ai_id))

    @app.post("/review/<int:ai_id>/skip")
    def review_skip(ai_id: int):
        c = conn()
        load_row(ai_id)
        c.execute(
            "UPDATE ai_fields SET status = 'skipped', skip_reason = COALESCE(NULLIF(?, ''), 'skipped in review'),"
            " reviewed_at = ? WHERE id = ?",
            (request.form.get("note", "").strip()[:300], db.iso(db.now()), ai_id),
        )
        c.commit()
        flash("Skipped. This lead won't get emails that use AI fields from this playbook.", "info")
        return review_redirect(next_waiting(c, ai_id))

    @app.post("/review/approve-clean")
    def review_approve_clean():
        c = conn()
        rows = c.execute("SELECT * FROM ai_fields WHERE status = 'needs_review'").fetchall()
        done = 0
        for row in rows:
            if all(ch.get("ok") for ch in json.loads(row["checks"] or "[]")):
                approve(c, row)
                done += 1
        c.commit()
        flash(f"Approved {done} drafts that passed every check.", "success")
        return review_redirect()
