"""Deployment configuration, read from environment variables (and an optional .env file).

Everything that is deployment-specific or secret lives here. Day-to-day settings
(sending window, limits, sheet URL, ...) are edited in the UI and stored in the database.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    pass


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment variables win."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    secret_key: str
    admin_password: str
    base_url: str = "http://localhost:8000"
    database_path: str = "data/outreach-wise.db"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uris: tuple[str, ...] = ()  # as registered, when read from client_secret.json
    # Microsoft 365 / Outlook.com sign-in (an app registration in Microsoft Entra).
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    run_worker: bool = True
    worker_tick_seconds: int = 15
    # Optional AI personalization keys. When set, they win over the keys saved in Settings.
    anthropic_api_key: str = ""
    youtube_api_key: str = ""

    @property
    def oauth_redirect_uri(self) -> str:
        """/oauth/callback, unless the client JSON already registers another URI on BASE_URL."""
        for uri in self.google_redirect_uris:
            if uri.startswith(self.base_url + "/"):
                return uri
        return f"{self.base_url}/oauth/callback"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_configured(self) -> bool:
        return bool(self.microsoft_client_id and self.microsoft_client_secret)

    @property
    def microsoft_redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/microsoft/callback"

    @property
    def redirect_uri_mismatch(self) -> bool:
        """The client JSON lists its redirect URIs and ours isn't among them."""
        return bool(self.google_redirect_uris) and self.oauth_redirect_uri not in self.google_redirect_uris

    @property
    def is_public(self) -> bool:
        """True when BASE_URL is reachable by recipients (HTTPS, not localhost)."""
        url = urlparse(self.base_url)
        return url.scheme == "https" and url.hostname not in ("localhost", "127.0.0.1", "::1")


def load_client_secrets(value: str) -> tuple[str, str, tuple[str, ...]]:
    """Read a Google OAuth client JSON (the file downloaded from Google Cloud), given as a
    path or as the JSON itself. Returns (client_id, client_secret, redirect_uris)."""
    try:
        text = value if value.lstrip().startswith("{") else Path(value).read_text()
        data = json.loads(text)
    except (OSError, ValueError) as err:
        raise ConfigError(f"GOOGLE_CLIENT_SECRETS_JSON could not be read: {err}") from None
    client = data.get("web") or data.get("installed") or data
    if not client.get("client_id") or not client.get("client_secret"):
        raise ConfigError("GOOGLE_CLIENT_SECRETS_JSON has no client_id/client_secret.")
    return client["client_id"], client["client_secret"], tuple(client.get("redirect_uris", ()))


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    secret = env.get("SECRET_KEY", "")
    password = env.get("ADMIN_PASSWORD", "")
    if len(secret) < 32:
        raise ConfigError(
            "SECRET_KEY must be set to a random string of at least 32 characters.\n"
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if len(password) < 8:
        raise ConfigError("ADMIN_PASSWORD must be set (at least 8 characters).")
    client_id, client_secret = env.get("GOOGLE_CLIENT_ID", ""), env.get("GOOGLE_CLIENT_SECRET", "")
    redirect_uris: tuple[str, ...] = ()
    if env.get("GOOGLE_CLIENT_SECRETS_JSON") and not (client_id and client_secret):
        client_id, client_secret, redirect_uris = load_client_secrets(env["GOOGLE_CLIENT_SECRETS_JSON"])
    return Config(
        secret_key=secret,
        admin_password=password,
        base_url=env.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        database_path=env.get("DATABASE_PATH", "data/outreach-wise.db"),
        google_client_id=client_id,
        google_client_secret=client_secret,
        google_redirect_uris=redirect_uris,
        microsoft_client_id=env.get("MICROSOFT_CLIENT_ID", "").strip(),
        microsoft_client_secret=env.get("MICROSOFT_CLIENT_SECRET", "").strip(),
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8000")),
        run_worker=_bool(env.get("RUN_WORKER", "true")),
        worker_tick_seconds=int(env.get("WORKER_TICK_SECONDS", "15")),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
        youtube_api_key=env.get("YOUTUBE_API_KEY", "").strip(),
    )
