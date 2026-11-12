"""API keys for AI personalization. Environment variables win over keys saved in Settings,
which are stored encrypted with SECRET_KEY."""

import sqlite3

from .. import db
from ..config import Config
from ..security import decrypt, encrypt

NAMES = {"ai_api_key": "anthropic_api_key", "youtube_api_key": "youtube_api_key"}


def from_env(cfg: Config, name: str) -> bool:
    return bool(getattr(cfg, NAMES[name]))


def get(conn: sqlite3.Connection, cfg: Config, name: str) -> str:
    env = getattr(cfg, NAMES[name])
    if env:
        return env
    stored = db.get_kv(conn, name)
    if not stored:
        return ""
    try:
        return decrypt(cfg.secret_key, stored)
    except ValueError:
        return ""


def set_key(conn: sqlite3.Connection, cfg: Config, name: str, value: str) -> None:
    value = value.strip()
    if value:
        db.set_kv(conn, name, encrypt(cfg.secret_key, value))
    else:
        conn.execute("DELETE FROM kv WHERE key = ?", (name,))


def masked(conn: sqlite3.Connection, cfg: Config, name: str) -> str:
    value = get(conn, cfg, name)
    return f"…{value[-4:]}" if len(value) > 8 else ("set" if value else "")
