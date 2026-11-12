"""Settings → Inboxes over SMTP/IMAP, the autoconfig lookup and the domain health card."""

import pytest
from test_web import client, login  # noqa: F401  (fixture + helper)

from outreach_wise import autoconfig, db, deliverability, smtp_web
from outreach_wise.security import decrypt
from outreach_wise.transports import AuthError, SmtpImapTransport

FORM = {
    "email": "Orhun@Acme.com",
    "password": "app-password",
    "smtp_host": "smtp.zoho.com",
    "smtp_port": "465",
    "smtp_security": "ssl",
    "imap_host": "imap.zoho.com",
    "imap_port": "993",
    "imap_security": "ssl",
}


@pytest.fixture
def servers(monkeypatch):
    """Stand-in for the real SMTP/IMAP logins; set `fail` to make one reject."""
    state = {"fail": None, "calls": []}

    def check_smtp(self):
        state["calls"].append(("smtp", self.config.password))
        if state["fail"] == "smtp":
            raise AuthError("535 authentication failed")

    def check_imap(self):
        state["calls"].append(("imap", self.config.password))
        if state["fail"] == "imap":
            raise AuthError("LOGIN failed")
        return "Sent"

    monkeypatch.setattr(SmtpImapTransport, "check_smtp", check_smtp)
    monkeypatch.setattr(SmtpImapTransport, "check_imap", check_imap)
    return state


def test_test_connection_reports_each_step(client, servers):  # noqa: F811
    csrf = login(client)
    result = client.post("/accounts/smtp/test", data={**FORM, "csrf": csrf}).get_json()
    assert result["ok"] and [s["ok"] for s in result["steps"]] == [True, True]
    assert result["steps"][1]["detail"] == "Sent folder: Sent"

    servers["fail"] = "smtp"
    result = client.post("/accounts/smtp/test", data={**FORM, "csrf": csrf}).get_json()
    assert not result["ok"] and "app-specific password" in result["steps"][0]["detail"]  # Zoho hint
    assert len(result["steps"]) == 1  # IMAP isn't tried after SMTP fails


def test_unencrypted_connections_are_refused(client, servers):  # noqa: F811
    csrf = login(client)
    response = client.post("/accounts/smtp/test", data={**FORM, "smtp_security": "none", "csrf": csrf})
    assert response.status_code == 400 and "clear text" in response.get_json()["steps"][0]["detail"]
    assert servers["calls"] == []


def test_save_tests_then_stores_an_encrypted_inbox(client, conn, cfg, servers):  # noqa: F811
    csrf = login(client)
    response = client.post("/accounts/smtp", data={**FORM, "csrf": csrf})
    assert response.status_code == 302 and response.headers["Location"].endswith("#inboxes")
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert (row["email"], row["transport"], row["smtp_port"], row["imap_host"]) == (
        "orhun@acme.com",
        "smtp_imap",
        465,
        "imap.zoho.com",
    )
    assert row["secret"] != "app-password" and decrypt(cfg.secret_key, row["secret"]) == "app-password"
    assert row["daily_limit"] == db.get_settings(conn)["daily_limit"]

    # Saving again (e.g. a new app password) updates the same inbox and re-enables it.
    conn.execute("UPDATE accounts SET enabled = 0, last_error = 'login failed'")
    conn.commit()
    client.post("/accounts/smtp", data={**FORM, "password": "new-password", "csrf": csrf})
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert (row["enabled"], row["last_error"]) == (1, None)
    assert decrypt(cfg.secret_key, row["secret"]) == "new-password"
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_save_refuses_when_the_login_fails(client, conn, servers):  # noqa: F811
    servers["fail"] = "imap"
    csrf = login(client)
    response = client.post("/accounts/smtp", data={**FORM, "csrf": csrf}, follow_redirects=True)
    assert b"Inbox not added" in response.data
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_save_does_not_take_over_a_google_inbox(client, conn, servers, account):  # noqa: F811
    csrf = login(client)
    response = client.post(
        "/accounts/smtp", data={**FORM, "email": account["email"], "csrf": csrf}, follow_redirects=True
    )
    assert b"already connected with Google" in response.data
    assert conn.execute("SELECT transport FROM accounts").fetchone()[0] == "gmail_api"


def test_autoconfig_route(client, monkeypatch):  # noqa: F811
    login(client)
    found = autoconfig.Settings(
        "Zoho", "a@acme.com", "smtp.zoho.com", 465, "ssl", "imap.zoho.com", source="ispdb"
    )
    monkeypatch.setattr(autoconfig, "lookup", lambda email: found if email == "a@acme.com" else None)
    assert client.get("/accounts/smtp/autoconfig?email=a@acme.com").get_json()["smtp_host"] == "smtp.zoho.com"
    assert client.get("/accounts/smtp/autoconfig?email=x@nowhere.test").get_json() == {"found": False}


def test_settings_page_shows_smtp_inbox_and_domain_health(client, conn, servers):  # noqa: F811
    csrf = login(client)
    client.post("/accounts/smtp", data={**FORM, "csrf": csrf})
    page = client.get("/settings").data.decode()
    assert "SMTP smtp.zoho.com" in page and "Add an inbox with email + password" in page
    assert "Not checked yet" in page

    db.set_kv(
        conn,
        "deliverability",
        {
            "checked_at": db.iso(db.now()),
            "domains": {"acme.com": [{"check": "SPF", "status": "fail", "detail": "Add include:zoho.com"}]},
        },
    )
    conn.commit()
    page = client.get("/settings").data.decode()
    assert "acme.com" in page and "Add include:zoho.com" in page and "badge bad" in page


def test_check_now_runs_the_deliverability_check(client, conn, account, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        deliverability,
        "check_domain",
        lambda domain, providers: [{"check": "DKIM", "status": "warn", "detail": "x"}],
    )
    csrf = login(client)
    response = client.post("/deliverability/check", data={"csrf": csrf}, follow_redirects=True)
    assert b"1 to fix" in response.data
    assert db.get_kv(conn, "deliverability")["domains"]["sender.io"][0]["check"] == "DKIM"


# --- autoconfig parsing ----------------------------------------------------------------------

OUTLOOK = b"""<?xml version="1.0"?>
<clientConfig version="1.1"><emailProvider id="outlook.com"><domain>outlook.com</domain>
<displayName>Outlook.com</displayName><displayShortName>Outlook</displayShortName>
<incomingServer type="pop3"><hostname>outlook.office365.com</hostname><port>995</port><socketType>SSL</socketType></incomingServer>
<incomingServer type="imap"><hostname>outlook.office365.com</hostname><port>993</port><socketType>SSL</socketType>
<authentication>OAuth2</authentication><username>%EMAILADDRESS%</username></incomingServer>
<outgoingServer type="smtp"><hostname>smtp-mail.outlook.com</hostname><port>587</port><socketType>STARTTLS</socketType>
<authentication>OAuth2</authentication><username>%EMAILADDRESS%</username></outgoingServer>
</emailProvider></clientConfig>"""

LOCALPART = b"""<clientConfig version="1.1"><emailProvider id="example"><displayName>Example Mail</displayName>
<outgoingServer type="smtp"><hostname>mail.%EMAILDOMAIN%</hostname><port>25</port><socketType>plain</socketType>
<authentication>password-cleartext</authentication><username>%EMAILLOCALPART%</username></outgoingServer>
<outgoingServer type="smtp"><hostname>mail.%EMAILDOMAIN%</hostname><port>465</port><socketType>SSL</socketType>
<authentication>password-cleartext</authentication><username>%EMAILLOCALPART%</username></outgoingServer>
</emailProvider></clientConfig>"""


def test_parse_ispdb_entry():
    found = autoconfig.parse(OUTLOOK, "me@outlook.com", "ispdb")
    assert (found.provider, found.smtp_host, found.smtp_port, found.smtp_security) == (
        "Outlook",
        "smtp-mail.outlook.com",
        587,
        "starttls",
    )
    assert (found.imap_host, found.imap_port, found.imap_security) == ("outlook.office365.com", 993, "ssl")
    assert found.oauth_preferred and found.username == "me@outlook.com"


def test_parse_prefers_encrypted_servers_and_fills_placeholders():
    found = autoconfig.parse(LOCALPART, "jane@acme.com")
    assert (found.smtp_host, found.smtp_port, found.smtp_security, found.username) == (
        "mail.acme.com",
        465,
        "ssl",
        "jane",
    )
    assert found.imap_host == "" and not found.oauth_preferred


def test_parse_rejects_garbage():
    assert autoconfig.parse(b"<html>not found</html>", "a@b.com") is None
    assert autoconfig.parse(b"not xml", "a@b.com") is None


def test_hints_name_the_provider_specific_fix():
    config = smtp_web._form_config({**FORM, "smtp_host": "smtp.gmail.com"})
    assert "app password" in smtp_web._hint(config)
