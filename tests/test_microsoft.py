"""Microsoft 365 sign-in: OAuth token handling, XOAUTH2 over SMTP, and the connect flow."""

import base64
import json
import socket
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from conftest import make_campaign
from test_web import client, login  # noqa: F401  (fixture + helper)

from outreach_wise import db, microsoft, sender
from outreach_wise.config import Config
from outreach_wise.security import decrypt, encrypt
from outreach_wise.transports import AuthError, MailboxConfig, SmtpImapTransport


def _id_token(**claims) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class TokenEndpoint:
    """Fake login.microsoftonline.com token endpoint that rotates refresh tokens."""

    def __init__(self):
        self.calls = []
        self.rotation = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.calls.append({k: v[0] for k, v in form.items()})
        grant = form["grant_type"][0]
        if grant == "authorization_code":
            if form["code"][0] != "good-code":
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "AADSTS70000: bad code"}
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "at-0",
                    "refresh_token": "rt-0",
                    "expires_in": 3600,
                    "scope": " ".join(microsoft.SCOPES),
                    "id_token": _id_token(preferred_username="Orhun@Contoso.com"),
                },
            )
        if form["refresh_token"][0] == "revoked":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "AADSTS700082: expired"}
            )
        self.rotation += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"at-{self.rotation}",
                "refresh_token": f"rt-{self.rotation}",
                "expires_in": 3600,
            },
        )


@pytest.fixture
def endpoint():
    return TokenEndpoint()


@pytest.fixture
def ms(endpoint, monkeypatch):
    instance = microsoft.Microsoft(
        "client-id",
        "client-secret",
        "http://localhost/oauth/microsoft/callback",
        httpx.Client(transport=httpx.MockTransport(endpoint)),
    )
    monkeypatch.setattr(microsoft, "_client", instance)
    return instance


def test_auth_url_asks_for_smtp_imap_and_offline_access(ms):
    url = urlparse(ms.auth_url("state-1", "me@contoso.com"))
    query = parse_qs(url.query)
    assert url.netloc == "login.microsoftonline.com"
    assert set(query["scope"][0].split()) >= {
        "offline_access",
        "https://outlook.office.com/SMTP.Send",
        "https://outlook.office.com/IMAP.AccessAsUser.All",
    }
    assert query["state"] == ["state-1"] and query["login_hint"] == ["me@contoso.com"]


def test_exchange_code_reads_the_address_from_the_id_token(ms):
    result = ms.exchange_code("good-code")
    assert result["email"] == "orhun@contoso.com" and result["refresh_token"] == "rt-0"
    with pytest.raises(microsoft.MicrosoftAuthError, match="bad code"):
        ms.exchange_code("bad-code")


def test_access_tokens_are_cached_and_rotations_followed(ms, endpoint):
    assert ms.access_token("rt-0") == "at-1"
    assert ms.access_token("rt-0") == "at-1"  # cached
    assert len(endpoint.calls) == 1
    assert ms.access_token("rt-0", force=True) == "at-2"
    # The second refresh used the rotated token, not the original one.
    assert endpoint.calls[-1]["refresh_token"] == "rt-1"
    with pytest.raises(microsoft.MicrosoftAuthError):
        ms.access_token("revoked")


def test_rotated_refresh_tokens_are_persisted(ms, conn, cfg):
    conn.execute(
        "INSERT INTO accounts (email, refresh_token, transport, created_at) VALUES ('me@contoso.com', ?, 'microsoft', ?)",
        (encrypt(cfg.secret_key, "rt-0"), db.iso(db.now())),
    )
    conn.commit()
    ms.access_token("rt-0")
    ms.access_token("rt-0", force=True)
    assert ms.persist_rotated(conn, cfg.secret_key) == 1
    stored = conn.execute("SELECT refresh_token FROM accounts").fetchone()[0]
    assert decrypt(cfg.secret_key, stored) == "rt-2"


def test_config_reads_microsoft_credentials():
    from outreach_wise.config import load_config

    cfg = load_config(
        {
            "SECRET_KEY": "x" * 40,
            "ADMIN_PASSWORD": "correct horse",
            "BASE_URL": "https://out.example.com/",
            "MICROSOFT_CLIENT_ID": "id",
            "MICROSOFT_CLIENT_SECRET": "secret",
        }
    )
    assert cfg.microsoft_configured
    assert cfg.microsoft_redirect_uri == "https://out.example.com/oauth/microsoft/callback"
    assert not Config(secret_key="x" * 40, admin_password="p" * 8).microsoft_configured


# --- XOAUTH2 against a real SMTP server ------------------------------------------------------


class XOAuthHandler:
    def __init__(self):
        self.logins = []
        self.messages = []

    async def auth_XOAUTH2(self, server, args):
        decoded = base64.b64decode(args[1]).decode()
        fields = dict(part.split("=", 1) for part in decoded.split("\x01") if part)
        self.logins.append(fields)
        return AuthResult(success=fields.get("auth", "").startswith("Bearer at-"), handled=False)

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope)
        return "250 OK"


@pytest.fixture
def xoauth_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handler = XOAuthHandler()
    controller = Controller(
        handler, hostname="127.0.0.1", port=port, auth_require_tls=False, server_hostname="localhost"
    )
    controller.start()
    yield controller, handler
    controller.stop()


def test_smtp_logs_in_with_xoauth2(xoauth_server):
    controller, handler = xoauth_server
    config = MailboxConfig(
        email="me@contoso.com",
        username="me@contoso.com",
        password="",
        smtp_host=controller.hostname,
        smtp_port=controller.port,
        smtp_security="none",
        timeout=5,
        access_token=lambda: "at-7",
    )
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "me@contoso.com", "lead@leads.io", "Hi"
    message["Message-ID"] = make_msgid(domain="contoso.com")
    message.set_content("Hello")
    SmtpImapTransport(config).send(message)
    assert handler.logins == [{"user": "me@contoso.com", "auth": "Bearer at-7"}]
    assert handler.messages[0].rcpt_tos == ["lead@leads.io"]


def test_expired_sign_in_is_an_auth_error_before_connecting():
    def expired():
        raise microsoft.MicrosoftAuthError("AADSTS700082: the refresh token has expired")

    config = MailboxConfig(
        email="me@contoso.com",
        username="me@contoso.com",
        password="",
        smtp_host="unused.invalid",
        access_token=expired,
    )
    with pytest.raises(AuthError, match="expired"):
        SmtpImapTransport(config).check_smtp()


def test_live_campaign_sends_from_a_microsoft_inbox(conn, cfg, clock, ms, xoauth_server):
    controller, handler = xoauth_server
    conn.execute(
        "INSERT INTO accounts (email, refresh_token, transport, smtp_host, smtp_port, smtp_security, daily_limit,"
        " created_at) VALUES ('me@contoso.com', ?, 'microsoft', ?, ?, 'none', 40, ?)",
        (encrypt(cfg.secret_key, "rt-0"), controller.hostname, controller.port, db.iso(db.now())),
    )
    conn.commit()
    make_campaign(conn, live=True, leads=1)
    assert sender.run_send_cycle(conn, cfg, None) == 1
    assert handler.logins[0]["auth"] == "Bearer at-1"
    received = message_from_bytes(handler.messages[0].original_content, policy=policy.default)
    assert received["Subject"] == "Hi Name1"


# --- connect flow ----------------------------------------------------------------------------


@pytest.fixture
def ms_web(client, ms, monkeypatch):  # noqa: F811
    monkeypatch.setattr(microsoft, "configure", lambda cfg: ms)
    state = {"fail": None}

    def check_smtp(self):
        assert self.config.access_token() == "at-1"
        if state["fail"] == "smtp":
            raise AuthError("535 5.7.139 Authentication unsuccessful, SmtpClientAuthentication is disabled")

    monkeypatch.setattr(SmtpImapTransport, "check_smtp", check_smtp)
    monkeypatch.setattr(SmtpImapTransport, "check_imap", lambda self: "Sent Items")
    return client, state


def _start(client):  # noqa: F811
    response = client.get("/accounts/microsoft/connect")
    assert response.status_code == 302 and "login.microsoftonline.com" in response.headers["Location"]
    return parse_qs(urlparse(response.headers["Location"]).query)["state"][0]


def test_connect_flow_saves_a_microsoft_inbox(ms_web, conn, cfg, monkeypatch):
    web, _ = ms_web
    monkeypatch.setattr(type(cfg), "microsoft_configured", property(lambda self: True))
    login(web)
    state = _start(web)
    response = web.get(f"/oauth/microsoft/callback?state={state}&code=good-code")
    assert response.status_code == 302
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert (row["email"], row["transport"], row["smtp_host"], row["imap_host"]) == (
        "orhun@contoso.com",
        "microsoft",
        "smtp.office365.com",
        "outlook.office365.com",
    )
    assert decrypt(cfg.secret_key, row["refresh_token"]) == "rt-0"
    assert "Microsoft 365" in web.get("/settings").data.decode()


def test_connect_flow_explains_disabled_smtp_auth(ms_web, conn, cfg, monkeypatch):
    web, state_ = ms_web
    state_["fail"] = "smtp"
    monkeypatch.setattr(type(cfg), "microsoft_configured", property(lambda self: True))
    login(web)
    state = _start(web)
    page = web.get(
        f"/oauth/microsoft/callback?state={state}&code=good-code", follow_redirects=True
    ).data.decode()
    assert "Authenticated SMTP" in page
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_callback_rejects_a_forged_state(ms_web, cfg, monkeypatch):
    web, _ = ms_web
    monkeypatch.setattr(type(cfg), "microsoft_configured", property(lambda self: True))
    login(web)
    _start(web)
    assert web.get("/oauth/microsoft/callback?state=forged&code=good-code").status_code == 400
