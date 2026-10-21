"""Background worker: a single loop that syncs the sheet, polls inboxes and sends.

Runs as a thread inside the web process by default (`outreach-wise serve`) or on its own
(`outreach-wise worker`). A lease lock in the database guarantees that only one worker is
active even if several processes are started.
"""

import logging
import os
import socket
import sqlite3
import threading
import uuid
from datetime import timedelta

from . import db, inbox, sender, sheets
from .config import Config
from .google import AuthError, Google, GoogleError

log = logging.getLogger("outreach_wise.worker")


def _due(conn: sqlite3.Connection, key: str, every_minutes: int) -> bool:
    last = db.get_kv(conn, key)
    return not last or db.parse(last) <= db.now() - timedelta(minutes=every_minutes)


def tick(conn: sqlite3.Connection, cfg: Config, google: Google) -> None:
    settings = db.get_settings(conn)

    if settings["sheet_url"] and _due(conn, "job:sync", settings["sync_interval_minutes"]):
        db.set_kv(conn, "job:sync", db.iso(db.now()))
        conn.commit()
        try:
            sheets.sync(conn, cfg, google)
        except (sheets.SyncError, GoogleError, ValueError) as err:
            conn.rollback()
            db.log_event(conn, "sync", f"Sheet sync failed: {err}", level="error")
            conn.commit()

    if _due(conn, "job:inbox", settings["inbox_poll_minutes"]):
        db.set_kv(conn, "job:inbox", db.iso(db.now()))
        conn.commit()
        for account in conn.execute("SELECT * FROM accounts WHERE enabled = 1").fetchall():
            try:
                inbox.poll(conn, cfg, google, account)
            except (AuthError, ValueError) as err:
                conn.rollback()
                inbox.disable_account(conn, account, err)
            except GoogleError as err:
                conn.rollback()
                conn.execute("UPDATE accounts SET last_error = ? WHERE id = ?", (str(err), account["id"]))
                conn.commit()
                log.warning("Inbox poll failed for %s: %s", account["email"], err)
            except Exception:
                conn.rollback()
                log.exception("Inbox poll crashed for %s", account["email"])

    sender.run_send_cycle(conn, cfg, google)


class Worker:
    def __init__(self, cfg: Config, google: Google):
        self.cfg = cfg
        self.google = google
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="outreach-wise-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def run(self) -> None:
        ttl = timedelta(seconds=max(120, self.cfg.worker_tick_seconds * 4))
        holding = False
        log.info("Worker started (%s)", self.owner)
        while not self._stop.is_set():
            conn = None
            try:
                conn = db.connect(self.cfg.database_path)
                if db.acquire_lease(conn, "worker", self.owner, ttl):
                    if not holding:
                        log.info("Worker lease acquired")
                        sender.recover_interrupted_sends(conn)
                        holding = True
                    tick(conn, self.cfg, self.google)
                elif holding is not None:
                    log.info(
                        "Another worker holds the lease; standing by (it expires after %ss)", ttl.seconds
                    )
                    holding = None
            except Exception:
                log.exception("Worker tick failed")
            finally:
                if conn:
                    conn.close()
            self._stop.wait(self.cfg.worker_tick_seconds)
        try:
            conn = db.connect(self.cfg.database_path)
            db.release_lease(conn, "worker", self.owner)
            conn.close()
        except sqlite3.Error:
            pass
        log.info("Worker stopped")
