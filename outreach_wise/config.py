"""Deployment configuration, read from environment variables (and an optional .env file).

Everything that is deployment-specific or secret lives here. Day-to-day settings
(sending window, limits, sheet URL, ...) are edited in the UI and stored in the database.
"""

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
    host: str = "127.0.0.1"
    port: int = 8000
    run_worker: bool = True
    worker_tick_seconds: int = 15

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/callback"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def is_public(self) -> bool:
        """True when BASE_URL is reachable by recipients (HTTPS, not localhost)."""
        url = urlparse(self.base_url)
        return url.scheme == "https" and url.hostname not in ("localhost", "127.0.0.1", "::1")


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
    return Config(
        secret_key=secret,
        admin_password=password,
        base_url=env.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        database_path=env.get("DATABASE_PATH", "data/outreach-wise.db"),
        google_client_id=env.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=env.get("GOOGLE_CLIENT_SECRET", ""),
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8000")),
        run_worker=_bool(env.get("RUN_WORKER", "true")),
        worker_tick_seconds=int(env.get("WORKER_TICK_SECONDS", "15")),
    )
