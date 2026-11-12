"""Email template library: saved ideas the user can paste into any step of any campaign."""

import sqlite3

from . import db
from .rendering import single_line

CATEGORIES = ("First email", "Follow-up", "Break-up", "Other")

# Starter ideas, inserted once when the table is created. Users can edit or delete them.
SEEDS = [
    (
        "Short & direct",
        "First email",
        "Quick question about {{channel_name|your channel}}",
        """Hi {{first_name|there}},

I came across {{channel_name|your channel}} and loved what you're building.

<one sentence on what you do and why it's relevant to them>

Would you be open to a quick chat this week?

Best,
<your name>""",
    ),
    (
        "Value first",
        "First email",
        "An idea for {{channel_name|your channel}}",
        """Hi {{first_name|there}},

I spent a few minutes on {{channel_name|your channel}} and had one idea that could help: <specific, concrete idea>.

We've helped <similar creator/company> do this and they saw <result>.

Happy to share how — worth a 15-minute call?

<your name>""",
    ),
    (
        "Social proof",
        "First email",
        "How <similar creator> grew <metric>",
        """Hi {{first_name|there}},

<similar creator> was in a similar spot to {{channel_name|you}} a few months ago. After working with us, they <result in numbers>.

I think there's a similar opportunity for you. Open to hearing how it worked?

Best,
<your name>""",
    ),
    (
        "One-line question",
        "First email",
        "{{first_name|Hey}}, quick one",
        """Hi {{first_name|there}},

Are you the right person to talk to about <topic> for {{channel_name|your channel}}?

<your name>""",
    ),
    (
        "Gentle bump",
        "Follow-up",
        "",
        """Hi {{first_name|there}},

Just bumping this up in case it got buried. Happy to share more details if it's useful.

Best,
<your name>""",
    ),
    (
        "Add new value",
        "Follow-up",
        "",
        """Hi {{first_name|there}},

One more thought since my last note: <new insight, resource or example>.

If the timing isn't right, no worries at all — just let me know.

<your name>""",
    ),
    (
        "Make it easy",
        "Follow-up",
        "",
        """Hi {{first_name|there}},

Would it help if I sent over a 2-minute summary instead of a call? Just reply "yes" and I'll send it.

<your name>""",
    ),
    (
        "Polite break-up",
        "Break-up",
        "",
        """Hi {{first_name|there}},

I haven't heard back, so I'll assume now isn't the right time and won't follow up again.

If anything changes, just reply to this email — I'd be glad to help.

All the best,
<your name>""",
    ),
]


def seed(conn: sqlite3.Connection) -> None:
    stamp = db.iso(db.now())
    conn.executemany(
        "INSERT INTO email_templates (name, category, subject, body, builtin, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 1, ?, ?)",
        [(*row, stamp, stamp) for row in SEEDS],
    )


def all_templates(conn: sqlite3.Connection) -> list[dict]:
    order = " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(CATEGORIES))
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM email_templates ORDER BY CASE category {order} ELSE 99 END, name COLLATE NOCASE"
        )
    ]


def _clean(name: str, category: str, subject: str, body: str) -> tuple[tuple, str | None]:
    name = single_line(name)[:120]
    category = category if category in CATEGORIES else "Other"
    subject = single_line(subject)[:250]
    body = body.replace("\r\n", "\n").strip("\n")
    if not name:
        return (), "Give the template a name."
    if not body.strip():
        return (), "The template body can't be empty."
    return (name, category, subject, body), None


def save(
    conn: sqlite3.Connection,
    name: str,
    category: str,
    subject: str,
    body: str,
    template_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Create (or update, given template_id). Returns (id, error)."""
    values, error = _clean(name, category, subject, body)
    if error:
        return None, error
    stamp = db.iso(db.now())
    if template_id is None:
        cur = conn.execute(
            "INSERT INTO email_templates (name, category, subject, body, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (*values, stamp, stamp),
        )
        return cur.lastrowid, None
    cur = conn.execute(
        "UPDATE email_templates SET name = ?, category = ?, subject = ?, body = ?, updated_at = ? WHERE id = ?",
        (*values, stamp, template_id),
    )
    return (template_id, None) if cur.rowcount else (None, "Template not found.")


def delete(conn: sqlite3.Connection, template_id: int) -> None:
    conn.execute("DELETE FROM email_templates WHERE id = ?", (template_id,))
