"""Microsoft 365 / Outlook.com sign-in (OAuth 2.0) for SMTP and IMAP over XOAUTH2.

Exchange Online turns off password sign-in for SMTP at the end of 2026, so Microsoft
mailboxes connect like Google ones: the user signs in once, we keep a refresh token
(encrypted) and trade it for short-lived access tokens that log in to
smtp.office365.com / outlook.office365.com.

Microsoft rotates refresh tokens: every refresh returns a new one. The old one keeps working
until it expires, so rotations are collected in memory and written to the database by the
worker (persist_rotated), never from inside a send.

Needs an app registration in Microsoft Entra (README → Microsoft setup) with the delegated
permissions below and MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET set.
"""

import base64
import hashlib
import json
import sqlite3
import threading
import time
from urllib.parse import urlencode

import httpx

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
SCOPES = [
    "offline_access",
    "openid",
    "email",
    "https://outlook.office.com/SMTP.Send",
    "https://outlook.office.com/IMAP.AccessAsUser.All",
]
SMTP_HOST, SMTP_PORT = "smtp.office365.com", 587
IMAP_HOST, IMAP_PORT = "outlook.office365.com", 993

_client: "Microsoft | None" = None


class MicrosoftError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class MicrosoftAuthError(MicrosoftError):
    """The stored sign-in no longer works; the inbox must be reconnected."""


def configure(cfg) -> "Microsoft | None":
    """Create the process-wide client from the configuration (None if not configured)."""
    global _client
    if cfg.microsoft_configured and (_client is None or _client.client_id != cfg.microsoft_client_id):
        _client = Microsoft(cfg.microsoft_client_id, cfg.microsoft_client_secret, cfg.microsoft_redirect_uri)
    return _client


def client() -> "Microsoft":
    if _client is None:
        raise MicrosoftAuthError("Microsoft sign-in is not configured (MICROSOFT_CLIENT_ID / _SECRET).")
    return _client


def _claims(id_token: str) -> dict:
    """The payload of an ID token received directly from Microsoft's token endpoint over TLS
    (so it needs no signature check to read the user's address)."""
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def _error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    return data.get("error_description", "").split("\r\n")[0] or data.get("error", "unknown error")


class Microsoft:
    def __init__(
        self, client_id: str, client_secret: str, redirect_uri: str, http: httpx.Client | None = None
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._tokens: dict[str, tuple[str, float]] = {}
        self._rotated: dict[str, str] = {}  # old refresh token -> newest one
        self._lock = threading.Lock()

    def auth_url(self, state: str, login_hint: str = "") -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(SCOPES),
            "prompt": "select_account",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTHORITY}/authorize?{urlencode(params)}"

    def _token(self, **data) -> dict:
        try:
            response = self.http.post(
                f"{AUTHORITY}/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": " ".join(SCOPES),
                    **data,
                },
            )
        except httpx.HTTPError as err:
            raise MicrosoftError(f"Network error talking to Microsoft: {err}", retryable=True) from err
        if response.status_code in (400, 401):
            raise MicrosoftAuthError(f"Microsoft rejected the sign-in: {_error(response)}")
        if response.status_code >= 400:
            raise MicrosoftError(f"Microsoft token endpoint failed ({response.status_code})", retryable=True)
        return response.json()

    def exchange_code(self, code: str) -> dict:
        """{"email", "refresh_token", "scopes"} for the account that just signed in."""
        data = self._token(grant_type="authorization_code", code=code, redirect_uri=self.redirect_uri)
        if not data.get("refresh_token"):
            raise MicrosoftAuthError("Microsoft did not return a refresh token (is offline_access allowed?).")
        claims = _claims(data.get("id_token", ""))
        email = (claims.get("email") or claims.get("preferred_username") or "").lower()
        if "@" not in email:
            raise MicrosoftAuthError("Could not read the mailbox address from the Microsoft sign-in.")
        return {"email": email, "refresh_token": data["refresh_token"], "scopes": data.get("scope", "")}

    def access_token(self, refresh_token: str, force: bool = False) -> str:
        refresh_token = self._rotated.get(refresh_token, refresh_token)
        key = hashlib.sha256(refresh_token.encode()).hexdigest()
        with self._lock:
            cached = self._tokens.get(key)
            if cached and not force and cached[1] > time.time() + 120:
                return cached[0]
        data = self._token(grant_type="refresh_token", refresh_token=refresh_token)
        with self._lock:
            self._tokens[key] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
            new = data.get("refresh_token")
            if new and new != refresh_token:
                for old, newest in list(self._rotated.items()):
                    if newest == refresh_token:
                        self._rotated[old] = new
                self._rotated[refresh_token] = new
                new_key = hashlib.sha256(new.encode()).hexdigest()
                self._tokens[new_key] = self._tokens[key]
        return data["access_token"]

    def persist_rotated(self, conn: sqlite3.Connection, secret_key: str) -> int:
        """Store rotated refresh tokens. Called by the worker between jobs."""
        from .security import decrypt, encrypt  # noqa: PLC0415

        with self._lock:
            rotated = dict(self._rotated)
        if not rotated:
            return 0
        saved = 0
        for row in conn.execute(
            "SELECT id, refresh_token FROM accounts WHERE transport = 'microsoft'"
        ).fetchall():
            try:
                current = decrypt(secret_key, row["refresh_token"])
            except ValueError:
                continue
            if current in rotated:
                conn.execute(
                    "UPDATE accounts SET refresh_token = ? WHERE id = ?",
                    (encrypt(secret_key, rotated[current]), row["id"]),
                )
                saved += 1
        conn.commit()
        # The old -> new mapping stays in memory: a transport built before this may still
        # hold the old token, and access_token() follows the mapping.
        return saved
