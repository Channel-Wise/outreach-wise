"""Web routes for SMTP/IMAP inboxes and the domain health report (Settings → Inboxes).

  GET  /accounts/smtp/autoconfig?email=…  server settings found for an address (JSON)
  POST /accounts/smtp/test                log in to SMTP and IMAP with the form's values (JSON)
  POST /accounts/smtp                     test again, then save (create or update by email)
  POST /deliverability/check              run the weekly SPF / DKIM / DMARC check now

Registered from web.create_app; the password is never sent back to the browser.
"""

import sqlite3

from flask import Flask, flash, g, jsonify, redirect, request, url_for

from . import autoconfig, db, deliverability
from .config import Config
from .security import encrypt
from .transports import AuthError, MailboxConfig, SmtpImapTransport, TransportError
from .transports.smtp_imap import SECURITY


def _form_config(form) -> MailboxConfig:
    """MailboxConfig from the add-inbox form. Raises ValueError with a message for the user."""
    email = form.get("email", "").strip().lower()
    if "@" not in email:
        raise ValueError("Enter the inbox's email address.")
    password = form.get("password", "")
    if not password:
        raise ValueError("Enter the password (or app password).")
    smtp_host = form.get("smtp_host", "").strip().lower()
    if not smtp_host:
        raise ValueError("Enter the SMTP server.")
    try:
        smtp_port = int(form.get("smtp_port") or 587)
        imap_port = int(form.get("imap_port") or 993)
    except ValueError:
        raise ValueError("Ports must be numbers.") from None
    for name in ("smtp_security", "imap_security"):
        if form.get(name) not in SECURITY or form.get(name) == "none":
            raise ValueError(
                "Use SSL or STARTTLS; unencrypted connections would send the password in clear text."
            )
    return MailboxConfig(
        email=email,
        username=form.get("username", "").strip() or email,
        password=password,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_security=form["smtp_security"],
        imap_host=form.get("imap_host", "").strip().lower(),
        imap_port=imap_port,
        imap_security=form["imap_security"],
        timeout=15,
    )


def _test(config: MailboxConfig) -> list[dict]:
    """[{step, ok, detail}] for SMTP login, then IMAP login and the Sent folder."""
    transport = SmtpImapTransport(config)
    steps = []

    def run(step, action):
        try:
            result = action()
        except AuthError as err:
            steps.append({"step": step, "ok": False, "detail": f"Login rejected. {_hint(config)} ({err})"})
            return False
        except (TransportError, OSError) as err:
            steps.append({"step": step, "ok": False, "detail": str(err)})
            return False
        steps.append({"step": step, "ok": True, "detail": f"Sent folder: {result}" if result else ""})
        return True

    if run(f"SMTP {config.smtp_host}:{config.smtp_port}", transport.check_smtp) and config.imap_host:
        run(f"IMAP {config.imap_host}:{config.imap_port}", transport.check_imap)
    if not config.imap_host:
        steps.append(
            {
                "step": "IMAP",
                "ok": None,
                "detail": "No IMAP server: replies can't be detected, so only first emails are sent from this inbox.",
            }
        )
    return steps


def _hint(config: MailboxConfig) -> str:
    host = config.smtp_host
    if "gmail" in host or "google" in host:
        return (
            "Gmail needs an app password (Google Account → Security → 2-Step Verification → App passwords)."
        )
    if "office365" in host or "outlook" in host:
        return "Microsoft 365 is turning off password sign-in for SMTP at the end of 2026; use Connect Google/Microsoft instead when available."
    if "zoho" in host:
        return "Zoho needs an app-specific password when two-factor sign-in is on."
    if "yandex" in host:
        return "Yandex needs an app password and IMAP enabled in Mail settings."
    return "Check the username and password; many providers require an app password."


def register(app: Flask, cfg: Config) -> None:
    def conn() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect(cfg.database_path)
        return g.db

    @app.context_processor
    def smtp_globals():
        return {"domain_health": lambda: db.get_kv(conn(), "deliverability")}

    @app.get("/accounts/smtp/autoconfig")
    def smtp_autoconfig():
        found = autoconfig.lookup(request.args.get("email", ""))
        return jsonify({"found": bool(found), **(found.as_dict() if found else {})})

    @app.post("/accounts/smtp/test")
    def smtp_test():
        try:
            config = _form_config(request.form)
        except ValueError as err:
            return jsonify({"ok": False, "steps": [{"step": "Form", "ok": False, "detail": str(err)}]}), 400
        steps = _test(config)
        return jsonify({"ok": all(s["ok"] is not False for s in steps), "steps": steps})

    @app.post("/accounts/smtp")
    def smtp_save():
        c = conn()
        try:
            config = _form_config(request.form)
        except ValueError as err:
            flash(f"Inbox not added: {err}", "error")
            return redirect(url_for("settings_page") + "#inboxes")
        failed = [s for s in _test(config) if s["ok"] is False]
        if failed:
            flash(f"Inbox not added: {failed[0]['step']}: {failed[0]['detail']}", "error")
            return redirect(url_for("settings_page") + "#inboxes")
        existing = c.execute("SELECT transport FROM accounts WHERE email = ?", (config.email,)).fetchone()
        if existing and existing["transport"] != "smtp_imap":
            flash(
                f"{config.email} is already connected with Google. Remove it first to use SMTP instead.",
                "error",
            )
            return redirect(url_for("settings_page") + "#inboxes")
        c.execute(
            """
            INSERT INTO accounts (email, refresh_token, transport, username, secret, smtp_host, smtp_port,
                                  smtp_security, imap_host, imap_port, imap_security, daily_limit, created_at)
            VALUES (?, '', 'smtp_imap', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET username = excluded.username, secret = excluded.secret,
                smtp_host = excluded.smtp_host, smtp_port = excluded.smtp_port,
                smtp_security = excluded.smtp_security, imap_host = excluded.imap_host,
                imap_port = excluded.imap_port, imap_security = excluded.imap_security,
                enabled = 1, last_error = NULL
            """,
            (
                config.email,
                config.username,
                encrypt(cfg.secret_key, config.password),
                config.smtp_host,
                config.smtp_port,
                config.smtp_security,
                config.imap_host,
                config.imap_port,
                config.imap_security,
                db.get_settings(c)["daily_limit"],
                db.iso(db.now()),
            ),
        )
        db.log_event(c, "account", f"Connected inbox {config.email} over SMTP ({config.smtp_host})")
        c.commit()
        flash(f"Connected {config.email}.", "success")
        return redirect(url_for("settings_page") + "#inboxes")

    @app.post("/deliverability/check")
    def deliverability_check():
        report = deliverability.run(conn())
        problems = sum(r["status"] != "ok" for results in report["domains"].values() for r in results)
        flash(
            f"Checked {len(report['domains'])} domain(s): "
            + (f"{problems} to fix." if problems else "all good."),
            "success" if not problems else "error",
        )
        return redirect(url_for("settings_page") + "#domain-health")
