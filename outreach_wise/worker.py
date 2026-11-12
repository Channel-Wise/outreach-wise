"""Background worker: a single loop that syncs the sheet, polls inboxes, checks the sending
domains weekly and sends.

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

from . import db, deliverability, inbox, microsoft, sender, sheets
from .config import Config
from .google import AuthError, Google, GoogleError

log = logging.getLogger("outreach_wise.worker")


def _due(conn: sqlite3.Connection, key: str, every_minutes: int) -> bool:
    last = db.get_kv(conn, key)
    return not last or db.parse(last) <= db.now() - timedelta(minutes=every_minutes)


def tick(conn: sqlite3.Connection, cfg: Config, google: Google) -> None:
    settings = db.get_settings(conn)
    ms = microsoft.configure(cfg)

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
        # SMTP inboxes without an IMAP server can't be read (the sender holds their follow-ups).
        readable = "SELECT * FROM accounts WHERE enabled = 1 AND (transport = 'gmail_api' OR imap_host <> '')"
        for account in conn.execute(readable).fetchall():
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

    if _due(conn, "job:deliverability", deliverability.INTERVAL_MINUTES):
        # Weekly SPF / DKIM / DMARC check of every sending domain (results in the activity log).
        db.set_kv(conn, "job:deliverability", db.iso(db.now()))
        conn.commit()
        try:
            deliverability.run(conn)
        except Exception:
            conn.rollback()
            log.exception("Deliverability check crashed")

    sender.run_send_cycle(conn, cfg, google)
    if ms:  # Microsoft rotates refresh tokens on use; keep the newest ones
        ms.persist_rotated(conn, cfg.secret_key)


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
        # AI research is slow (seconds to minutes per lead), so it runs beside the send loop.
        ai_worker = AIWorker(self.cfg, self._stop, self.owner)
        ai_worker.start()
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
        ai_worker.join(timeout=30)
        log.info("Worker stopped")


class AIWorker(threading.Thread):
    """Plans and writes AI fields (outreach_wise/ai/). Idle unless AI personalization is on.
    Holds its own "ai" lease so only one process calls the model at a time."""

    def __init__(self, cfg: Config, stop: threading.Event, owner: str):
        super().__init__(name="outreach-wise-ai", daemon=True)
        self.cfg, self._stop_event, self.owner = cfg, stop, owner

    def run(self) -> None:
        from .ai import scheduler  # noqa: PLC0415

        ttl = timedelta(minutes=10)
        recovered = False
        while not self._stop_event.is_set():
            conn = None
            try:
                conn = db.connect(self.cfg.database_path)
                settings = db.get_settings(conn)
                if settings["ai_enabled"] and db.acquire_lease(conn, "ai", self.owner, ttl):
                    if not recovered:
                        scheduler.recover(conn)
                        recovered = True
                    scheduler.plan(conn, settings)
                    # One lead at a time, renewing the lease in between.
                    while not self._stop_event.is_set() and scheduler.run(conn, self.cfg, settings):
                        db.acquire_lease(conn, "ai", self.owner, ttl)
                        settings = db.get_settings(conn)
                        if not settings["ai_enabled"]:
                            break
            except Exception:
                log.exception("AI worker tick failed")
            finally:
                if conn:
                    conn.close()
            self._stop_event.wait(max(self.cfg.worker_tick_seconds, 10))
        try:
            conn = db.connect(self.cfg.database_path)
            db.release_lease(conn, "ai", self.owner)
            conn.close()
        except sqlite3.Error:
            pass
