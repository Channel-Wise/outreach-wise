"""Web UI: server-rendered Flask app. Single admin user, password from ADMIN_PASSWORD."""

import json
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from . import campaigns, db, inbox, sender, sheets
from .config import Config
from .google import SCOPES, Google, GoogleError
from .security import encrypt, password_matches, read_unsubscribe_token

PUBLIC_ENDPOINTS = {"login", "unsubscribe", "healthz", "static"}
PAGE_SIZE = 50
LEAD_STATUSES = ("new", "contacted", "replied", "bounced", "unsubscribed")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def create_app(cfg: Config, google: Google | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_NAME="outreach_wise",
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.base_url.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    google = google or Google(cfg.google_client_id, cfg.google_client_secret, cfg.oauth_redirect_uri)
    failed_logins: dict[str, list[float]] = defaultdict(list)

    def conn():
        if "db" not in g:
            g.db = db.connect(cfg.database_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.before_request
    def guard():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not session.get("auth"):
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if request.method == "POST" and not secrets.compare_digest(
            request.form.get("csrf", ""), session.get("csrf", "")
        ):
            abort(400, "Invalid form token. Reload the page and try again.")
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.context_processor
    def template_globals():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        context = {"csrf_token": session["csrf"], "cfg": cfg, "weekdays": WEEKDAYS}
        if session.get("auth"):
            c = conn()
            context["settings"] = db.get_settings(c)
            context["inbox_count"] = c.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 1").fetchone()[
                0
            ]
        return context

    # --- template filters -------------------------------------------------------------------

    def tz() -> ZoneInfo:
        return ZoneInfo(db.get_settings(conn())["timezone"])

    @app.template_filter("local")
    def local_time(value: str | None, fmt: str = "%b %d, %H:%M") -> str:
        return db.parse(value).astimezone(tz()).strftime(fmt) if value else "—"

    @app.template_filter("pct")
    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    @app.template_filter("fields")
    def fields(value: str) -> dict:
        return json.loads(value or "{}")

    @app.template_filter("days")
    def days(values: list[int]) -> str:
        return ", ".join(WEEKDAYS[d] for d in values)

    def back(default: str):
        target = request.form.get("next") or default
        return redirect(target if target.startswith("/") and not target.startswith("//") else default)

    # --- auth -------------------------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            ip = request.remote_addr or "?"
            recent = [t for t in failed_logins[ip] if t > time.time() - 900]
            failed_logins[ip] = recent
            if len(recent) >= 10:
                error = "Too many attempts. Try again in 15 minutes."
            elif password_matches(request.form.get("password", ""), cfg.admin_password):
                failed_logins.pop(ip, None)
                session.clear()
                session.update(auth=True, csrf=secrets.token_urlsafe(32))
                session.permanent = True
                target = request.args.get("next", "/")
                return redirect(target if target.startswith("/") and not target.startswith("//") else "/")
            else:
                recent.append(time.time())
                time.sleep(1)
                error = "Wrong password."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def healthz():
        conn().execute("SELECT 1")
        return {"ok": True}

    # --- dashboard --------------------------------------------------------------------------

    @app.get("/")
    def dashboard():
        c = conn()
        settings = db.get_settings(c)
        moment = db.now()
        today = db.iso(sender.day_start(moment, settings))
        lead_counts = dict(c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
        accounts = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        reached = c.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM messages WHERE mode = 'live' AND status = 'sent'"
        ).fetchone()[0]
        replied = c.execute(
            "SELECT COUNT(*) FROM leads WHERE status = 'replied'"
            " AND id IN (SELECT lead_id FROM messages WHERE mode = 'live' AND status = 'sent')"
        ).fetchone()[0]
        totals = {
            "leads": sum(lead_counts.values()),
            "new_today": c.execute("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (today,)).fetchone()[
                0
            ],
            "sent_today": c.execute(
                "SELECT COUNT(*) FROM messages WHERE mode = 'live' AND status IN ('sent', 'sending', 'unknown')"
                " AND created_at >= ?",
                (today,),
            ).fetchone()[0],
            "capacity": sum(a["daily_limit"] for a in accounts if a["enabled"]),
            "reached": reached,
            "replied": replied,
            "reply_rate": replied / reached if reached else 0.0,
            "unsubscribed": lead_counts.get("unsubscribed", 0),
            "bounced": lead_counts.get("bounced", 0),
        }

        # Last 14 local days: emails sent and replies received.
        zone = ZoneInfo(settings["timezone"])
        days = [(moment.astimezone(zone).date() - timedelta(days=i)) for i in range(13, -1, -1)]
        since = db.iso(datetime.combine(days[0], datetime.min.time(), tzinfo=zone))
        series = {day: {"sent": 0, "replies": 0} for day in days}
        for (stamp,) in c.execute(
            "SELECT sent_at FROM messages WHERE mode = 'live' AND status = 'sent' AND sent_at >= ?", (since,)
        ):
            day = db.parse(stamp).astimezone(zone).date()
            if day in series:
                series[day]["sent"] += 1
        for (stamp,) in c.execute(
            "SELECT received_at FROM inbound WHERE kind = 'reply' AND received_at >= ?", (since,)
        ):
            day = db.parse(stamp).astimezone(zone).date()
            if day in series:
                series[day]["replies"] += 1
        chart = [
            {"label": day.strftime("%b %d"), "weekday": day.strftime("%a"), **series[day]} for day in days
        ]
        chart_top = _nice_ceiling(max(point["sent"] for point in chart))

        rows = []
        for campaign in c.execute(
            "SELECT * FROM campaigns ORDER BY status = 'active' DESC, id DESC"
        ).fetchall():
            rows.append({"campaign": campaign, "stats": campaigns.stats(c, campaign)})
        replies = c.execute(
            "SELECT i.*, a.email AS account_email FROM inbound i LEFT JOIN accounts a ON a.id = i.account_id"
            " WHERE i.kind IN ('reply', 'unsubscribe') ORDER BY i.received_at DESC LIMIT 8"
        ).fetchall()
        events = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8").fetchall()
        return render_template(
            "dashboard.html",
            totals=totals,
            chart=chart,
            chart_top=chart_top,
            campaign_rows=rows,
            replies=replies,
            events=events,
            accounts=accounts,
            in_window=sender.in_window(moment, settings),
            last_sync=db.get_kv(c, "last_sync"),
        )

    # --- leads ------------------------------------------------------------------------------

    @app.get("/leads")
    def leads():
        c = conn()
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        page = max(int(request.args.get("page", 1) or 1), 1)
        where, params = [], []
        if q:
            where.append("(email LIKE ? OR fields LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if status in LEAD_STATUSES:
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = c.execute(f"SELECT COUNT(*) FROM leads {clause}", params).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM leads {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        columns: list[str] = []
        for row in rows:
            for key in json.loads(row["fields"]):
                if key not in columns and "email" not in key and len(columns) < 4:
                    columns.append(key)
        counts = dict(c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
        return render_template(
            "leads.html",
            rows=rows,
            columns=columns,
            total=total,
            page=page,
            pages=max(1, -(-total // PAGE_SIZE)),
            q=q,
            status=status,
            statuses=LEAD_STATUSES,
            counts=counts,
            last_sync=db.get_kv(c, "last_sync"),
        )

    @app.get("/leads/<int:lead_id>")
    def lead(lead_id: int):
        c = conn()
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone() or abort(404)
        return render_template(
            "lead.html",
            lead=row,
            suppressed=db.is_suppressed(c, row["email"]),
            enrollments=c.execute(
                "SELECT e.*, c.name AS campaign_name, c.mode FROM enrollments e"
                " JOIN campaigns c ON c.id = e.campaign_id WHERE e.lead_id = ? ORDER BY e.id DESC",
                (lead_id,),
            ).fetchall(),
            messages=c.execute(
                "SELECT m.*, s.position, v.label FROM messages m JOIN steps s ON s.id = m.step_id"
                " JOIN variants v ON v.id = m.variant_id WHERE m.lead_id = ? ORDER BY m.id DESC",
                (lead_id,),
            ).fetchall(),
            inbound_rows=c.execute(
                "SELECT * FROM inbound WHERE lead_id = ? ORDER BY received_at DESC", (lead_id,)
            ).fetchall(),
        )

    @app.post("/leads/<int:lead_id>/suppress")
    def suppress_lead(lead_id: int):
        c = conn()
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone() or abort(404)
        db.suppress(c, row["email"], "manual")
        c.execute(
            "UPDATE enrollments SET status = 'stopped', stop_reason = 'suppressed' WHERE lead_id = ? AND status = 'active'",
            (lead_id,),
        )
        db.log_event(c, "suppression", f"Suppressed {row['email']}", lead_id=lead_id)
        c.commit()
        flash(f"{row['email']} will never be emailed.", "success")
        return redirect(url_for("lead", lead_id=lead_id))

    @app.post("/leads/sync")
    def sync_leads():
        c = conn()
        try:
            stats = sheets.sync(c, cfg, google)
            db.set_kv(c, "job:sync", db.iso(db.now()))
            c.commit()
            flash(
                f"Synced: {stats['created']} new, {stats['updated']} updated, {stats['duplicates']} duplicates and "
                f"{stats['invalid']} invalid rows skipped.",
                "success",
            )
        except (sheets.SyncError, GoogleError, ValueError) as err:
            c.rollback()
            flash(f"Sync failed: {err}", "error")
        return back(url_for("leads"))

    # --- campaigns --------------------------------------------------------------------------

    @app.get("/campaigns")
    def campaign_list():
        c = conn()
        rows = [
            {"campaign": row, "stats": campaigns.stats(c, row)}
            for row in c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        ]
        return render_template("campaigns.html", rows=rows)

    @app.post("/campaigns")
    def campaign_create():
        name = request.form.get("name", "").strip()[:120]
        if not name:
            flash("Give the campaign a name.", "error")
            return redirect(url_for("campaign_list"))
        c = conn()
        campaign_id = campaigns.create(c, name)
        c.commit()
        flash("Campaign created in dry-run mode. Write your emails, enroll leads, then start it.", "success")
        return redirect(url_for("campaign", campaign_id=campaign_id))

    def load_campaign(campaign_id: int):
        return campaigns.get(conn(), campaign_id) or abort(404)

    @app.get("/campaigns/<int:campaign_id>")
    def campaign(campaign_id: int):
        c = conn()
        row = load_campaign(campaign_id)
        start_local = (
            db.parse(row["start_at"]).astimezone(tz()).strftime("%Y-%m-%dT%H:%M") if row["start_at"] else ""
        )
        return render_template(
            "campaign.html",
            campaign=row,
            stats=campaigns.stats(c, row),
            ab=campaigns.ab_results(c, row),
            steps=campaigns.steps_with_variants(c, campaign_id),
            eligible=campaigns.count_eligible(c, campaign_id),
            eligible_contacted=campaigns.count_eligible(c, campaign_id, include_contacted=True),
            start_local=start_local,
            variables=_template_variables(c),
            stop_reasons=c.execute(
                "SELECT stop_reason, COUNT(*) AS n FROM enrollments WHERE campaign_id = ? AND status = 'stopped'"
                " GROUP BY stop_reason ORDER BY n DESC LIMIT 6",
                (campaign_id,),
            ).fetchall(),
            messages=c.execute(
                "SELECT m.*, s.position, v.label FROM messages m JOIN steps s ON s.id = m.step_id"
                " JOIN variants v ON v.id = m.variant_id WHERE m.campaign_id = ? AND m.mode = ?"
                " ORDER BY m.id DESC LIMIT 15",
                (campaign_id, row["mode"]),
            ).fetchall(),
        )

    @app.post("/campaigns/<int:campaign_id>/settings")
    def campaign_settings(campaign_id: int):
        c = conn()
        load_campaign(campaign_id)
        name = request.form.get("name", "").strip()[:120]
        start_at = None
        if request.form.get("start_at"):
            try:
                local = datetime.strptime(request.form["start_at"], "%Y-%m-%dT%H:%M").replace(tzinfo=tz())
                start_at = db.iso(local)
            except ValueError:
                flash("Invalid start date.", "error")
                return redirect(url_for("campaign", campaign_id=campaign_id))
        c.execute(
            "UPDATE campaigns SET name = COALESCE(NULLIF(?, ''), name), auto_enroll = ?, start_at = ? WHERE id = ?",
            (name, int(request.form.get("auto_enroll") == "on"), start_at, campaign_id),
        )
        c.commit()
        flash("Campaign settings saved.", "success")
        return redirect(url_for("campaign", campaign_id=campaign_id))

    @app.post("/campaigns/<int:campaign_id>/status")
    def campaign_status(campaign_id: int):
        c = conn()
        row = load_campaign(campaign_id)
        action = request.form.get("action")
        new_status = {"start": "active", "resume": "active", "pause": "paused"}.get(action)
        if not new_status:
            abort(400)
        c.execute("UPDATE campaigns SET status = ? WHERE id = ?", (new_status, campaign_id))
        mode = "LIVE" if row["mode"] == "live" else "dry run"
        verb = {"start": "Started", "resume": "Resumed", "pause": "Paused"}[action]
        db.log_event(c, "campaign", f'{verb} campaign "{row["name"]}" ({mode})', campaign_id=campaign_id)
        c.commit()
        flash(f"{verb} ({mode}).", "success")
        return redirect(url_for("campaign", campaign_id=campaign_id))

    @app.post("/campaigns/<int:campaign_id>/enroll")
    def campaign_enroll(campaign_id: int):
        c = conn()
        load_campaign(campaign_id)
        limit = request.form.get("limit", "").strip()
        count = campaigns.enroll(
            c,
            campaign_id,
            int(limit) if limit.isdigit() and int(limit) > 0 else None,
            include_contacted=request.form.get("include_contacted") == "on",
        )
        c.commit()
        flash(f"Enrolled {count} leads.", "success" if count else "info")
        return redirect(url_for("campaign", campaign_id=campaign_id))

    @app.post("/campaigns/<int:campaign_id>/steps")
    def step_add(campaign_id: int):
        c = conn()
        load_campaign(campaign_id)
        campaigns.add_step(c, campaign_id, delay_days=3)
        c.commit()
        return redirect(url_for("campaign", campaign_id=campaign_id) + "#sequence")

    @app.post("/campaigns/<int:campaign_id>/delete")
    def campaign_delete(campaign_id: int):
        c = conn()
        row = load_campaign(campaign_id)
        error = campaigns.delete(c, campaign_id)
        if error:
            flash(error, "error")
            return redirect(url_for("campaign", campaign_id=campaign_id))
        db.log_event(c, "campaign", f'Deleted campaign "{row["name"]}"')
        c.commit()
        flash("Campaign deleted.", "success")
        return redirect(url_for("campaign_list"))

    def step_campaign(step_id: int) -> int:
        row = conn().execute("SELECT campaign_id FROM steps WHERE id = ?", (step_id,)).fetchone() or abort(
            404
        )
        return row["campaign_id"]

    def variant_campaign(variant_id: int) -> int:
        row = conn().execute(
            "SELECT s.campaign_id FROM variants v JOIN steps s ON s.id = v.step_id WHERE v.id = ?",
            (variant_id,),
        ).fetchone() or abort(404)
        return row["campaign_id"]

    def done(campaign_id: int, error: str | None, success: str, anchor: str = "sequence"):
        if error:
            conn().rollback()
            flash(error, "error")
        else:
            conn().commit()
            flash(success, "success")
        return redirect(url_for("campaign", campaign_id=campaign_id) + f"#{anchor}")

    @app.post("/steps/<int:step_id>")
    def step_update(step_id: int):
        campaign_id = step_campaign(step_id)
        try:
            delay = max(0, min(int(request.form.get("delay_days", 3)), 365))
        except ValueError:
            return done(campaign_id, "Delay must be a number of days.", "")
        conn().execute("UPDATE steps SET delay_days = ? WHERE id = ? AND position > 1", (delay, step_id))
        return done(campaign_id, None, "Delay saved.", f"step-{step_id}")

    @app.post("/steps/<int:step_id>/delete")
    def step_delete(step_id: int):
        campaign_id = step_campaign(step_id)
        return done(campaign_id, campaigns.delete_step(conn(), step_id), "Step deleted.")

    @app.post("/steps/<int:step_id>/variants")
    def variant_add(step_id: int):
        campaign_id = step_campaign(step_id)
        return done(
            campaign_id,
            campaigns.add_variant(conn(), step_id),
            "Variant added. Edit it to test something different.",
            f"step-{step_id}",
        )

    @app.post("/variants/<int:variant_id>")
    def variant_update(variant_id: int):
        campaign_id = variant_campaign(variant_id)
        error = campaigns.update_variant(
            conn(),
            variant_id,
            request.form.get("subject", ""),
            request.form.get("body", ""),
            active=request.form.get("active") == "on",
        )
        return done(campaign_id, error, "Email saved.", f"variant-{variant_id}")

    @app.post("/variants/<int:variant_id>/delete")
    def variant_delete(variant_id: int):
        campaign_id = variant_campaign(variant_id)
        return done(campaign_id, campaigns.delete_variant(conn(), variant_id), "Variant deleted.")

    @app.get("/campaigns/<int:campaign_id>/preview")
    def campaign_preview(campaign_id: int):
        c = conn()
        row = load_campaign(campaign_id)
        settings = db.get_settings(c)
        return render_template(
            "preview.html",
            campaign=row,
            preview=campaigns.preview(c, cfg, campaign_id, settings["footer"]),
            blockers=campaigns.go_live_blockers(c, campaign_id),
            accounts=c.execute("SELECT * FROM accounts WHERE enabled = 1 ORDER BY id").fetchall(),
            dry_run_sent=c.execute(
                "SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND mode = 'dry_run'", (campaign_id,)
            ).fetchone()[0],
        )

    @app.post("/campaigns/<int:campaign_id>/go-live")
    def campaign_go_live(campaign_id: int):
        c = conn()
        row = load_campaign(campaign_id)
        if row["mode"] == "live":
            return redirect(url_for("campaign", campaign_id=campaign_id))
        blockers = campaigns.go_live_blockers(c, campaign_id)
        if blockers:
            flash(blockers[0], "error")
            return redirect(url_for("campaign_preview", campaign_id=campaign_id))
        if (
            request.form.get("approve") != "yes"
            or request.form.get("confirm_name", "").strip() != row["name"]
        ):
            flash("To approve live sending, tick the box and type the campaign name exactly.", "error")
            return redirect(url_for("campaign_preview", campaign_id=campaign_id))
        count = campaigns.go_live(c, campaign_id)
        c.commit()
        flash(
            f"Approved. {count} leads will receive real emails within your sending window and limits.",
            "success",
        )
        return redirect(url_for("campaign", campaign_id=campaign_id))

    # --- activity & suppressions -----------------------------------------------------------

    @app.get("/activity")
    def activity():
        c = conn()
        kind = request.args.get("kind", "")
        level = request.args.get("level", "")
        page = max(int(request.args.get("page", 1) or 1), 1)
        where, params = [], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if level:
            where.append("level = ?")
            params.append(level)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = c.execute(f"SELECT COUNT(*) FROM events {clause}", params).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        kinds = [r[0] for r in c.execute("SELECT DISTINCT kind FROM events ORDER BY kind")]
        return render_template(
            "activity.html",
            rows=rows,
            kinds=kinds,
            kind=kind,
            level=level,
            page=page,
            pages=max(1, -(-total // PAGE_SIZE)),
            total=total,
        )

    @app.get("/suppressions")
    def suppressions():
        c = conn()
        q = request.args.get("q", "").strip().lower()
        rows = c.execute(
            "SELECT * FROM suppressions WHERE value LIKE ? ORDER BY id DESC LIMIT 500", (f"%{q}%",)
        ).fetchall()
        total = c.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0]
        return render_template("suppressions.html", rows=rows, total=total, q=q)

    @app.post("/suppressions")
    def suppression_add():
        c = conn()
        added = invalid = 0
        for line in re.split(r"[\s,;]+", request.form.get("values", "")):
            value = line.strip().lower()
            if not value:
                continue
            if "@" not in value:
                value = "@" + value
            if not re.fullmatch(r"[^@\s]*@[^@\s]+\.[^@\s]+", value):
                invalid += 1
                continue
            added += db.suppress(c, value, "manual")
        if added:
            db.log_event(c, "suppression", f"Added {added} suppression entries")
        c.commit()
        flash(f"Added {added} entries" + (f", skipped {invalid} invalid" if invalid else "") + ".", "success")
        return redirect(url_for("suppressions"))

    @app.post("/suppressions/<int:suppression_id>/delete")
    def suppression_delete(suppression_id: int):
        c = conn()
        row = c.execute("SELECT * FROM suppressions WHERE id = ?", (suppression_id,)).fetchone() or abort(404)
        c.execute("DELETE FROM suppressions WHERE id = ?", (suppression_id,))
        db.log_event(c, "suppression", f"Removed {row['value']} from suppressions")
        c.commit()
        flash(f"Removed {row['value']}.", "success")
        return redirect(url_for("suppressions"))

    # --- settings & inboxes -----------------------------------------------------------------

    @app.get("/settings")
    def settings_page():
        c = conn()
        return render_template(
            "settings.html",
            accounts=c.execute("SELECT * FROM accounts ORDER BY id").fetchall(),
            last_sync=db.get_kv(c, "last_sync"),
            redirect_uri=cfg.oauth_redirect_uri,
            timezones=["UTC"]
            + sorted(
                z
                for z in available_timezones()
                if "/" in z and not z.startswith(("Etc/", "SystemV/", "build/"))
            ),
        )

    @app.post("/settings")
    def settings_save():
        c = conn()
        form = request.form
        section = form.get("section")
        updates: dict = {}
        try:
            if section == "sheet":
                updates = {
                    "sheet_url": form.get("sheet_url", "").strip(),
                    "sheet_tab": form.get("sheet_tab", "").strip() or "Sheet1",
                    "sheet_email_column": form.get("sheet_email_column", "").strip() or "email",
                    "sync_interval_minutes": _bounded(form, "sync_interval_minutes", 5, 1440),
                }
            elif section == "sending":
                ZoneInfo(form.get("timezone", "UTC"))
                start, end = form.get("window_start", ""), form.get("window_end", "")
                if not (
                    re.fullmatch(r"\d\d:\d\d", start) and re.fullmatch(r"\d\d:\d\d", end) and start < end
                ):
                    raise ValueError("The sending window must end after it starts.")
                days = sorted({int(d) for d in form.getlist("send_days") if d.isdigit() and int(d) < 7})
                if not days:
                    raise ValueError("Pick at least one sending day.")
                updates = {
                    "timezone": form["timezone"],
                    "window_start": start,
                    "window_end": end,
                    "send_days": days,
                    "daily_limit": _bounded(form, "daily_limit", 1, 2000),
                    "min_gap_seconds": _bounded(form, "min_gap_seconds", 10, 3600),
                    "inbox_poll_minutes": _bounded(form, "inbox_poll_minutes", 1, 60),
                }
            elif section == "email":
                updates = {
                    "from_name": " ".join(form.get("from_name", "").split())[:100],
                    "footer": form.get("footer", "").replace("\r\n", "\n"),
                }
            else:
                abort(400)
        except (ValueError, ZoneInfoNotFoundError) as err:
            flash(f"Not saved: {err or 'invalid value'}", "error")
            return redirect(url_for("settings_page"))
        db.save_settings(c, updates)
        db.log_event(c, "settings", f"Updated {section} settings")
        c.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page") + f"#{section}")

    @app.post("/settings/pause")
    def toggle_pause():
        c = conn()
        paused = not db.get_settings(c)["sending_paused"]
        db.save_settings(c, {"sending_paused": paused})
        db.log_event(c, "settings", "All sending PAUSED" if paused else "Sending resumed", level="warning")
        c.commit()
        flash("All sending is paused." if paused else "Sending resumed.", "success")
        return back(url_for("dashboard"))

    @app.get("/accounts/connect")
    def account_connect():
        if not cfg.google_configured:
            flash("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (see README → Google setup).", "error")
            return redirect(url_for("settings_page"))
        session["oauth_state"] = secrets.token_urlsafe(24)
        return redirect(google.auth_url(session["oauth_state"]))

    @app.get("/oauth/callback")
    def oauth_callback():
        expected = session.pop("oauth_state", None)
        if not expected or not secrets.compare_digest(request.args.get("state", ""), expected):
            abort(400, "OAuth state mismatch. Start the connection again.")
        if request.args.get("error"):
            flash(f"Google connection cancelled ({request.args['error']}).", "error")
            return redirect(url_for("settings_page"))
        c = conn()
        try:
            result = google.exchange_code(request.args.get("code", ""))
        except GoogleError as err:
            flash(str(err), "error")
            return redirect(url_for("settings_page"))
        missing = [scope.rsplit("/", 1)[-1] for scope in SCOPES if scope not in result["scopes"].split()]
        if missing:
            flash(f"Please allow all requested permissions (missing: {', '.join(missing)}).", "error")
            return redirect(url_for("settings_page"))
        c.execute(
            "INSERT INTO accounts (email, refresh_token, scopes, daily_limit, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(email) DO UPDATE SET refresh_token = excluded.refresh_token, scopes = excluded.scopes,"
            " enabled = 1, last_error = NULL",
            (
                result["email"],
                encrypt(cfg.secret_key, result["refresh_token"]),
                result["scopes"],
                db.get_settings(c)["daily_limit"],
                db.iso(db.now()),
            ),
        )
        db.log_event(c, "account", f"Connected inbox {result['email']}")
        c.commit()
        flash(f"Connected {result['email']}.", "success")
        return redirect(url_for("settings_page"))

    @app.post("/accounts/<int:account_id>")
    def account_update(account_id: int):
        c = conn()
        row = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone() or abort(404)
        try:
            limit = _bounded(request.form, "daily_limit", 1, 2000)
        except ValueError as err:
            flash(str(err), "error")
            return redirect(url_for("settings_page"))
        enabled = request.form.get("enabled") == "on"
        c.execute(
            "UPDATE accounts SET daily_limit = ?, enabled = ?, last_error = CASE WHEN ? THEN NULL ELSE last_error END"
            " WHERE id = ?",
            (limit, int(enabled), int(enabled and not row["enabled"]), account_id),
        )
        db.log_event(
            c,
            "account",
            f"Updated inbox {row['email']} (limit {limit}/day, {'enabled' if enabled else 'disabled'})",
            account_id=account_id,
        )
        c.commit()
        flash("Inbox updated.", "success")
        return redirect(url_for("settings_page") + "#inboxes")

    @app.post("/accounts/<int:account_id>/delete")
    def account_delete(account_id: int):
        c = conn()
        row = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone() or abort(404)
        # Follow-ups must come from the same inbox/thread; stop sequences that already started there.
        c.execute(
            "UPDATE enrollments SET status = 'stopped', stop_reason = 'inbox removed'"
            " WHERE account_id = ? AND status = 'active'",
            (account_id,),
        )
        c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        db.log_event(c, "account", f"Removed inbox {row['email']}")
        c.commit()
        flash(f"Removed {row['email']}. Also revoke access at myaccount.google.com/permissions.", "success")
        return redirect(url_for("settings_page"))

    # --- public: unsubscribe ----------------------------------------------------------------

    @app.route("/u/<token>", methods=["GET", "POST"])
    def unsubscribe(token: str):
        c = conn()
        lead_id = read_unsubscribe_token(cfg.secret_key, token)
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone() if lead_id else None
        sender_name = db.get_settings(c)["from_name"] or "us"
        if row is None:
            return render_template("unsubscribe.html", state="invalid", sender_name=sender_name), 404
        if request.method == "POST":  # also RFC 8058 one-click unsubscribe from mail clients
            if row["status"] != "unsubscribed":
                inbox.apply_outcome(c, row["id"], "unsubscribe", db.now(), note="via unsubscribe link")
                c.commit()
            return render_template("unsubscribe.html", state="done", sender_name=sender_name)
        return render_template("unsubscribe.html", state="confirm", token=token, sender_name=sender_name)

    return app


def _template_variables(conn) -> list[str]:
    """Column names seen on recent leads, offered as {{variables}} in the editor."""
    keys = {"email"}
    for (fields,) in conn.execute("SELECT fields FROM leads ORDER BY id DESC LIMIT 200"):
        keys.update(json.loads(fields))
    return sorted(keys) + ["unsubscribe_url"]


def _bounded(form, key: str, low: int, high: int) -> int:
    try:
        value = int(form.get(key, ""))
    except ValueError:
        raise ValueError(f"{key.replace('_', ' ')} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{key.replace('_', ' ')} must be between {low} and {high}")
    return value


def _nice_ceiling(value: int) -> int:
    """Smallest of 10, 20, 50, 100, 200, 500, ... that is >= value (a clean axis maximum)."""
    magnitude = 10
    while True:
        for step in (1, 2, 5):
            if step * magnitude >= value:
                return step * magnitude
        magnitude *= 10
