"""SQLite storage: connection setup, schema migrations, settings, activity log and small helpers.

Plain SQL on purpose. All timestamps are stored as UTC strings "YYYY-MM-DD HH:MM:SS",
which sort and compare correctly as text.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Append new migrations to the end of this list; never edit an applied one.
MIGRATIONS = [
    """
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Connected Gmail / Google Workspace inboxes. refresh_token is encrypted with SECRET_KEY.
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        refresh_token TEXT NOT NULL,
        scopes TEXT NOT NULL DEFAULT '',
        daily_limit INTEGER NOT NULL DEFAULT 40,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_error TEXT,
        next_send_at TEXT,
        inbox_checked_at TEXT,
        created_at TEXT NOT NULL
    );

    -- One row per unique (normalized) email. fields = all sheet columns as JSON.
    -- status: new | contacted | replied | bounced | unsubscribed
    CREATE TABLE leads (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        fields TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX leads_status ON leads(status);

    -- value is a full address ("jane@acme.com") or a whole domain ("@acme.com").
    CREATE TABLE suppressions (
        id INTEGER PRIMARY KEY,
        value TEXT NOT NULL UNIQUE,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    -- status: draft | active | paused     mode: dry_run | live
    CREATE TABLE campaigns (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        mode TEXT NOT NULL DEFAULT 'dry_run',
        auto_enroll INTEGER NOT NULL DEFAULT 0,
        start_at TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL
    );

    -- delay_days = days to wait after the previous step (ignored for step 1).
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY,
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        delay_days INTEGER NOT NULL DEFAULT 3,
        UNIQUE (campaign_id, position)
    );

    -- A/B variants of a step. Empty subject on a follow-up = reply in the same thread.
    CREATE TABLE variants (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    );

    -- A lead's progress through one campaign.
    -- status: active | completed | replied | bounced | unsubscribed | stopped
    CREATE TABLE enrollments (
        id INTEGER PRIMARY KEY,
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
        account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'active',
        next_step INTEGER NOT NULL DEFAULT 1,
        next_send_at TEXT NOT NULL,
        thread_id TEXT,
        last_message_id TEXT,
        last_subject TEXT,
        stop_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (campaign_id, lead_id)
    );
    CREATE INDEX enrollments_due ON enrollments(status, next_send_at);
    CREATE INDEX enrollments_lead ON enrollments(lead_id);

    -- Every email we send (or dry-run). UNIQUE(enrollment_id, step_id) is the guard
    -- against duplicate sends: a row is claimed *before* Gmail is called.
    -- status: sending | sent | dry_run | failed | unknown
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
        campaign_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL,
        step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
        variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
        account_id INTEGER,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        to_email TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        gmail_id TEXT,
        gmail_thread_id TEXT,
        rfc_message_id TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        sent_at TEXT,
        replied_at TEXT,
        UNIQUE (enrollment_id, step_id)
    );
    CREATE INDEX messages_thread ON messages(gmail_thread_id);
    CREATE INDEX messages_daily ON messages(account_id, mode, created_at);
    CREATE INDEX messages_campaign ON messages(campaign_id, mode);

    -- Inbound Gmail messages we have already looked at.
    -- kind: reply | unsubscribe | bounce | auto_reply | ignored
    CREATE TABLE inbound (
        gmail_id TEXT PRIMARY KEY,
        account_id INTEGER,
        thread_id TEXT,
        kind TEXT NOT NULL,
        from_email TEXT,
        subject TEXT,
        snippet TEXT,
        lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
        enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
        received_at TEXT NOT NULL
    );
    CREATE INDEX inbound_kind ON inbound(kind, received_at);

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        lead_id INTEGER,
        campaign_id INTEGER,
        account_id INTEGER
    );
    CREATE INDEX events_created ON events(created_at);

    -- Lease lock so only one process runs background jobs at a time.
    CREATE TABLE locks (
        name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """,
    """
    -- Saved email ideas the user can paste into any step (seeded by library.seed()).
    CREATE TABLE email_templates (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'First email',
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        builtin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    -- Branching follow-ups: a variant can be reserved for leads who got a specific variant
    -- of the previous step. NULL = everyone without a dedicated variant.
    ALTER TABLE variants ADD COLUMN after_variant_id INTEGER REFERENCES variants(id) ON DELETE SET NULL;
    """,
    """
    -- Inboxes connected over SMTP/IMAP with a username + (app) password (see transports/).
    -- transport: gmail_api | smtp_imap. For smtp_imap, refresh_token is '' and `secret` holds
    -- the password, encrypted with SECRET_KEY. (Columns are added rather than rebuilding the
    -- table: dropping `accounts` would null enrollments.account_id through its foreign key.)
    ALTER TABLE accounts ADD COLUMN transport TEXT NOT NULL DEFAULT 'gmail_api';
    ALTER TABLE accounts ADD COLUMN username TEXT;
    ALTER TABLE accounts ADD COLUMN secret TEXT;
    ALTER TABLE accounts ADD COLUMN smtp_host TEXT;
    ALTER TABLE accounts ADD COLUMN smtp_port INTEGER NOT NULL DEFAULT 587;
    ALTER TABLE accounts ADD COLUMN smtp_security TEXT NOT NULL DEFAULT 'starttls';
    ALTER TABLE accounts ADD COLUMN imap_host TEXT;
    ALTER TABLE accounts ADD COLUMN imap_port INTEGER NOT NULL DEFAULT 993;
    ALTER TABLE accounts ADD COLUMN imap_security TEXT NOT NULL DEFAULT 'ssl';
    ALTER TABLE accounts ADD COLUMN save_sent INTEGER;  -- NULL: decide from the SMTP host
    ALTER TABLE accounts ADD COLUMN sent_folder TEXT;
    -- Replies to SMTP-sent mail are matched by In-Reply-To / References.
    CREATE INDEX messages_rfc_message_id ON messages(rfc_message_id);
    """,
    """
    -- Branching follow-ups, many-to-many: a follow-up variant is sent to leads who got any of its
    -- linked variants of the previous step. A variant without links goes to "everyone else"
    -- (leads whose previous variant has no linked follow-up). Replaces variants.after_variant_id,
    -- which is left in place but no longer used.
    CREATE TABLE variant_links (
        variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
        parent_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
        PRIMARY KEY (variant_id, parent_id)
    );
    CREATE INDEX variant_links_parent ON variant_links(parent_id);
    INSERT INTO variant_links (variant_id, parent_id)
        SELECT id, after_variant_id FROM variants WHERE after_variant_id IS NOT NULL;
    UPDATE variants SET after_variant_id = NULL;
    """,
    """
    -- AI personalization (optional, see outreach_wise/ai/). A playbook is stored as its full
    -- .playbook.md text: TOML settings between +++ lines, then Markdown instructions.
    CREATE TABLE playbooks (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        source TEXT NOT NULL,
        builtin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Public data fetched for a lead (YouTube channel, web page), shared by all playbooks.
    CREATE TABLE lead_research (
        lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        input TEXT NOT NULL,
        data TEXT,
        error TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (lead_id, source, input)
    );

    -- What a playbook wrote for a lead. One row per (lead, playbook): reused by every campaign
    -- that uses the same playbook.
    -- status: pending | researching | needs_review | approved | skipped | failed
    CREATE TABLE ai_fields (
        id INTEGER PRIMARY KEY,
        lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
        playbook_id INTEGER NOT NULL REFERENCES playbooks(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        vals TEXT NOT NULL DEFAULT '{}',
        original_vals TEXT,
        alternatives TEXT NOT NULL DEFAULT '{}',
        evidence TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        skip_reason TEXT,
        checks TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        regenerate_note TEXT,
        playbook_hash TEXT,
        provider TEXT,
        model TEXT,
        usage TEXT,
        cost_usd REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        generated_at TEXT,
        reviewed_at TEXT,
        UNIQUE (lead_id, playbook_id)
    );
    CREATE INDEX ai_fields_status ON ai_fields(status);

    -- Reviewer edits (before -> after), fed back to the playbook as examples.
    CREATE TABLE ai_examples (
        id INTEGER PRIMARY KEY,
        playbook_id INTEGER NOT NULL REFERENCES playbooks(id) ON DELETE CASCADE,
        field TEXT NOT NULL,
        before TEXT NOT NULL,
        after TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    ALTER TABLE campaigns ADD COLUMN playbook_id INTEGER REFERENCES playbooks(id) ON DELETE SET NULL;
    ALTER TABLE campaigns ADD COLUMN require_approval INTEGER NOT NULL DEFAULT 1;
    -- The variant chosen ahead of time for step `pinned_step`, so we know before sending
    -- whether this lead needs AI fields at all.
    ALTER TABLE enrollments ADD COLUMN pinned_variant_id INTEGER REFERENCES variants(id) ON DELETE SET NULL;
    ALTER TABLE enrollments ADD COLUMN pinned_step INTEGER;
    """,
    """
    -- Editable A/B splits. A lead's share of each option is weight / sum of the options' weights:
    -- variants.weight for the first email and "everyone else" follow-ups, variant_links.weight
    -- for a follow-up connected to a specific previous variant (so one follow-up can take 30%
    -- of A's leads and 90% of B's). Equal weights = even split.
    ALTER TABLE variants ADD COLUMN weight INTEGER NOT NULL DEFAULT 100;
    ALTER TABLE variant_links ADD COLUMN weight INTEGER NOT NULL DEFAULT 100;
    """,
]

DEFAULT_SETTINGS = {
    "timezone": "UTC",
    "window_start": "09:00",
    "window_end": "17:00",
    "send_days": [0, 1, 2, 3, 4],  # Monday = 0
    "daily_limit": 40,  # default per connected inbox
    "min_gap_seconds": 60,
    "from_name": "",
    "footer": '\n\n--\nNot relevant for you? Just reply "unsubscribe" and I won\'t email you again.',
    "sheet_url": "",
    "sheet_tab": "Sheet1",
    "sheet_email_column": "email",
    "sync_interval_minutes": 30,
    "inbox_poll_minutes": 5,
    "sending_paused": False,
}


# --- time -----------------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TIME_FORMAT)


def parse(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)


# --- connection -----------------------------------------------------------------------------


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    if version < 2 <= len(MIGRATIONS):
        from . import library  # noqa: PLC0415 — avoid an import cycle

        library.seed(conn)
        conn.commit()


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        migrate(conn)
    finally:
        conn.close()


# --- settings & job state -------------------------------------------------------------------


def get_kv(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_kv(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def get_settings(conn: sqlite3.Connection) -> dict:
    return {**DEFAULT_SETTINGS, **get_kv(conn, "settings", {})}


def save_settings(conn: sqlite3.Connection, updates: dict) -> None:
    current = get_kv(conn, "settings", {})
    current.update({k: v for k, v in updates.items() if k in DEFAULT_SETTINGS})
    set_kv(conn, "settings", current)


# --- activity log ---------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    level: str = "info",
    lead_id: int | None = None,
    campaign_id: int | None = None,
    account_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (created_at, level, kind, message, lead_id, campaign_id, account_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (iso(now()), level, kind, message, lead_id, campaign_id, account_id),
    )


# --- lease lock -----------------------------------------------------------------------------


def acquire_lease(conn: sqlite3.Connection, name: str, owner: str, ttl: timedelta) -> bool:
    """Take or renew a named lease. Returns True if `owner` holds it afterwards."""
    current = now()
    cur = conn.execute(
        "INSERT INTO locks (name, owner, expires_at) VALUES (?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at"
        " WHERE locks.owner = excluded.owner OR locks.expires_at < ?",
        (name, owner, iso(current + ttl), iso(current)),
    )
    conn.commit()
    return cur.rowcount == 1


def release_lease(conn: sqlite3.Connection, name: str, owner: str) -> None:
    conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))
    conn.commit()


# --- suppression ----------------------------------------------------------------------------


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1]


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM suppressions WHERE value IN (?, ?)", (email, "@" + email_domain(email))
        ).fetchone()
        is not None
    )


def suppress(conn: sqlite3.Connection, value: str, reason: str) -> bool:
    cur = conn.execute(
        "INSERT INTO suppressions (value, reason, created_at) VALUES (?, ?, ?) ON CONFLICT(value) DO NOTHING",
        (value.strip().lower(), reason, iso(now())),
    )
    return cur.rowcount == 1
