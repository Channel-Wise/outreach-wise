import base64
import email
from datetime import UTC, datetime, timedelta

import pytest

from outreach_wise import campaigns, db, sheets
from outreach_wise.config import Config
from outreach_wise.security import encrypt

# A Wednesday, 10:00 UTC: inside the default Mon–Fri 09:00–17:00 window.
START = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
SENDER = "me@sender.io"


class Clock:
    def __init__(self, start: datetime):
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta) -> datetime:
        self.value += timedelta(**delta)
        return self.value


class FakeGoogle:
    """In-memory stand-in for outreach_wise.google.Google."""

    def __init__(self):
        self.sent: list[dict] = []
        self.messages: dict[str, dict] = {}
        self.threads: dict[str, list[dict]] = {}
        self.inbox: list[dict] = []
        self.fail_next: Exception | None = None
        self.sheet: list[list[str]] = []

    def send(self, refresh_token, raw, thread_id=None):
        assert refresh_token == "refresh-token"
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error
        number = len(self.sent) + 1
        gmail_id, thread = f"g{number}", thread_id or f"t{number}"
        mime = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        self.sent.append({"id": gmail_id, "thread_id": thread, "mime": mime})
        message = {
            "id": gmail_id,
            "thread_id": thread,
            "snippet": "",
            "internal_date": 0,
            "labels": ["SENT"],
            "headers": {"from": SENDER, "message-id": f"<{gmail_id}@mail.gmail.com>"},
        }
        self.messages[gmail_id] = message
        self.threads.setdefault(thread, []).append(message)
        return {"id": gmail_id, "threadId": thread}

    def deliver(self, *, thread_id, sender, subject="Re: hi", snippet="Sounds good!", headers=None, at=None):
        gmail_id = f"in{len(self.inbox) + 1}"
        message = {
            "id": gmail_id,
            "thread_id": thread_id,
            "snippet": snippet,
            "internal_date": int((at or datetime.now(UTC)).timestamp()),
            "labels": ["INBOX"],
            "headers": {"from": sender, "subject": subject, **(headers or {})},
        }
        self.inbox.append(message)
        self.messages[gmail_id] = message
        self.threads.setdefault(thread_id, []).append(message)
        return message

    def message(self, refresh_token, message_id):
        return self.messages[message_id]

    def thread(self, refresh_token, thread_id):
        return list(self.threads.get(thread_id, []))

    def list_messages(self, refresh_token, query, limit=500):
        return [{"id": m["id"], "threadId": m["thread_id"]} for m in reversed(self.inbox)]

    def sheet_values(self, refresh_token, spreadsheet_id, tab):
        return self.sheet


@pytest.fixture
def cfg(tmp_path):
    return Config(
        secret_key="x" * 40, admin_password="correct horse", database_path=str(tmp_path / "test.db")
    )


@pytest.fixture
def clock(monkeypatch):
    clock = Clock(START)
    monkeypatch.setattr(db, "now", clock)
    return clock


@pytest.fixture
def conn(cfg, clock):
    db.init_db(cfg.database_path)
    connection = db.connect(cfg.database_path)
    yield connection
    connection.close()


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def account(conn, cfg):
    conn.execute(
        "INSERT INTO accounts (email, refresh_token, daily_limit, created_at) VALUES (?, ?, 40, ?)",
        (SENDER, encrypt(cfg.secret_key, "refresh-token"), db.iso(db.now())),
    )
    conn.commit()
    return conn.execute("SELECT * FROM accounts WHERE email = ?", (SENDER,)).fetchone()


def add_leads(conn, count=3, domain="leads.io", **extra):
    rows = [["Email", "First Name", "Channel Name", *extra.keys()]]
    for i in range(count):
        rows.append([f"lead{i + 1}@{domain}", f"Name{i + 1}", f"Channel {i + 1}", *extra.values()])
    leads, _ = sheets.parse_rows(rows)
    sheets.upsert_leads(conn, leads)
    conn.commit()


def make_campaign(conn, *, live=False, leads=3, delay_days=3):
    """A two-step campaign (first email + threaded follow-up) with `leads` enrolled and started."""
    add_leads(conn, leads)
    campaign_id = campaigns.create(conn, "Test campaign")
    steps = campaigns.steps_with_variants(conn, campaign_id)
    campaigns.update_variant(
        conn, steps[0]["variants"][0]["id"], "Hi {{first_name}}", "Hello {{first_name}}!", True
    )
    conn.execute("UPDATE steps SET delay_days = ? WHERE id = ?", (delay_days, steps[1]["id"]))
    campaigns.enroll(conn, campaign_id)
    conn.execute("UPDATE campaigns SET status = 'active' WHERE id = ?", (campaign_id,))
    if live:
        campaigns.go_live(conn, campaign_id)
    conn.commit()
    return campaign_id
