"""A thin client for the Google APIs we use: OAuth 2.0, Gmail and Sheets, over plain HTTPS.

Methods that act on an inbox take that inbox's (decrypted) refresh token. Access tokens
are cached in memory and refreshed automatically.
"""

import hashlib
import threading
import time
from urllib.parse import quote, urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GoogleError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AuthError(GoogleError):
    """The stored credentials no longer work; the inbox must be reconnected."""


class SendStateUnknown(Exception):
    """The send request may or may not have reached Gmail (e.g. a read timeout)."""


def _error_text(response: httpx.Response) -> tuple[str, str]:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return response.text[:200], ""
    if isinstance(error, str):  # OAuth token endpoint style
        return error, error
    reasons = [e.get("reason", "") for e in error.get("errors", [])]
    return error.get("message", "unknown error"), " ".join(reasons + [error.get("status", "")])


class Google:
    def __init__(
        self, client_id: str, client_secret: str, redirect_uri: str, http: httpx.Client | None = None
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # --- OAuth ------------------------------------------------------------------------------

    def auth_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict:
        """Returns {"email", "refresh_token", "scopes"} for the account that just consented."""
        response = self.http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if response.status_code >= 400:
            raise AuthError(f"Google rejected the authorization: {_error_text(response)[0]}")
        data = response.json()
        if not data.get("refresh_token"):
            raise AuthError("Google did not return a refresh token. Remove the app's access and try again.")
        profile = self.http.get(
            f"{GMAIL}/profile", headers={"Authorization": f"Bearer {data['access_token']}"}
        )
        if profile.status_code >= 400:
            raise AuthError(f"Could not read the Gmail profile: {_error_text(profile)[0]}")
        return {
            "email": profile.json()["emailAddress"].lower(),
            "refresh_token": data["refresh_token"],
            "scopes": data.get("scope", ""),
        }

    def access_token(self, refresh_token: str, force: bool = False) -> str:
        key = hashlib.sha256(refresh_token.encode()).hexdigest()
        with self._lock:
            cached = self._tokens.get(key)
            if cached and not force and cached[1] > time.time() + 60:
                return cached[0]
        try:
            response = self.http.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as err:
            raise GoogleError(f"Network error while refreshing token: {err}", retryable=True) from err
        if response.status_code in (400, 401):
            raise AuthError(
                f"Google rejected the stored credentials ({_error_text(response)[0]}). Reconnect."
            )
        if response.status_code >= 400:
            raise GoogleError("Token refresh failed", response.status_code, retryable=True)
        data = response.json()
        with self._lock:
            self._tokens[key] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return data["access_token"]

    # --- low-level request ------------------------------------------------------------------

    def _call(self, refresh_token: str, method: str, url: str, *, is_send: bool = False, **kwargs) -> dict:
        for attempt in (1, 2):
            token = self.access_token(refresh_token, force=attempt == 2)
            try:
                response = self.http.request(
                    method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as err:
                # The request never reached Google: always safe to retry later.
                raise GoogleError(f"Network error: {err}", retryable=True) from err
            except httpx.HTTPError as err:
                # The request may have been processed (read timeout, dropped connection...).
                if is_send:
                    raise SendStateUnknown(str(err) or type(err).__name__) from err
                raise GoogleError(f"Network error: {err}", retryable=True) from err
            if response.status_code == 401 and attempt == 1:
                continue  # expired access token: the request was rejected, refresh and retry once
            break
        if response.status_code < 400:
            return response.json()
        message, reasons = _error_text(response)
        if response.status_code == 401:
            raise AuthError(message, 401)
        if response.status_code == 403:
            if any(r in reasons.lower() for r in ("ratelimit", "dailylimit", "quota")):
                raise GoogleError(f"Rate limited by Google: {message}", 403, retryable=True)
            if "insufficientpermissions" in reasons.lower() or "permission_denied" in reasons.lower():
                raise AuthError(f"Missing permission: {message}", 403)
        raise GoogleError(message, response.status_code, retryable=response.status_code in RETRYABLE_STATUS)

    # --- Gmail ------------------------------------------------------------------------------

    def send(self, refresh_token: str, raw: str, thread_id: str | None = None) -> dict:
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self._call(refresh_token, "POST", f"{GMAIL}/messages/send", json=body, is_send=True)

    def message(self, refresh_token: str, message_id: str) -> dict:
        data = self._call(
            refresh_token,
            "GET",
            f"{GMAIL}/messages/{message_id}",
            params=[("format", "metadata")] + [("metadataHeaders", h) for h in _HEADERS],
        )
        return _normalize_message(data)

    def list_messages(self, refresh_token: str, query: str, limit: int = 500) -> list[dict]:
        """[{"id", "threadId"}, ...], newest first."""
        found, page_token = [], None
        while len(found) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(found))}
            if page_token:
                params["pageToken"] = page_token
            data = self._call(refresh_token, "GET", f"{GMAIL}/messages", params=params)
            found.extend(data.get("messages", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return found

    def thread(self, refresh_token: str, thread_id: str) -> list[dict]:
        data = self._call(
            refresh_token,
            "GET",
            f"{GMAIL}/threads/{thread_id}",
            params=[("format", "metadata")] + [("metadataHeaders", h) for h in _HEADERS],
        )
        return [_normalize_message(m) for m in data.get("messages", [])]

    # --- Sheets -----------------------------------------------------------------------------

    def sheet_values(self, refresh_token: str, spreadsheet_id: str, tab: str) -> list[list[str]]:
        sheet_range = quote("'" + tab.replace("'", "''") + "'", safe="")
        data = self._call(refresh_token, "GET", f"{SHEETS}/{spreadsheet_id}/values/{sheet_range}")
        return data.get("values", [])


_HEADERS = (
    "From",
    "To",
    "Subject",
    "Message-ID",
    "Auto-Submitted",
    "X-Autoreply",
    "X-Autorespond",
    "Precedence",
)


def _normalize_message(data: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    return {
        "id": data["id"],
        "thread_id": data.get("threadId"),
        "snippet": data.get("snippet", ""),
        "internal_date": int(data.get("internalDate", 0)) // 1000,
        "labels": data.get("labelIds", []),
        "headers": headers,
    }
