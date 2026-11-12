"""Playbooks: how the AI researches a lead and which parts of the email it writes.

A playbook is one text file (`*.playbook.md`): TOML settings between `+++` lines, then the
instructions in plain Markdown.

    +++
    name = "YouTube creators"
    language = "auto"

    [input]
    column = "channel_url"
    sources = ["youtube_channel"]

    [fields.hook]
    describe = "1-3 short sentences about one concrete thing on their channel"
    max_words = 60

    [checks]
    email_words = [60, 140]
    banned = ["quick question"]
    +++

    # Instructions
    ...

Each field becomes the template variable `ai_<name>` (`{{ai_hook}}`).
"""

import hashlib
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..rendering import variables_in

SOURCES = {
    "youtube_channel": "YouTube channel",
    "web_page": "Web page",
}
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "playbooks"
MAX_SOURCE_CHARS = 200_000


class PlaybookError(ValueError):
    pass


@dataclass
class Field:
    name: str
    describe: str = ""
    max_words: int | None = None
    required: bool = True
    fallback: str | None = None
    alternatives: int = 0

    @property
    def variable(self) -> str:
        return f"ai_{self.name}"


@dataclass
class Checks:
    email_words: tuple[int, int] | None = None
    subject_words: tuple[int, int] | None = None
    banned: list[str] = field(default_factory=list)
    one_link: bool = False
    numbers_in_data: bool = False


@dataclass
class Playbook:
    name: str
    input_column: str
    sources: list[str]
    fields: list[Field]
    instructions: str = ""
    language: str = "auto"
    checks: Checks = field(default_factory=Checks)

    def get_field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def variables(self) -> list[str]:
        return [f.variable for f in self.fields]


# --- parsing --------------------------------------------------------------------------------


def split(text: str) -> tuple[str, str]:
    """(TOML settings, Markdown instructions)."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    if not text.lstrip().startswith("+++"):
        raise PlaybookError("A playbook starts with a settings block between two +++ lines.")
    body = text.lstrip()[3:]
    end = re.search(r"^\+\+\+[ \t]*$", body, re.MULTILINE)
    if not end:
        raise PlaybookError("The settings block isn't closed with a +++ line.")
    return body[: end.start()], body[end.end() :].strip("\n")


def _range(value, label: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, int) and v >= 0 for v in value)
        or value[0] > value[1]
    ):
        raise PlaybookError(f"checks.{label} must look like [min, max].")
    return value[0], value[1]


def parse(text: str) -> Playbook:
    if len(text) > MAX_SOURCE_CHARS:
        raise PlaybookError("This playbook is too long (200,000 characters at most).")
    settings_text, instructions = split(text)
    try:
        data = tomllib.loads(settings_text)
    except tomllib.TOMLDecodeError as err:
        raise PlaybookError(f"Settings block: {err}") from None

    name = str(data.get("name", "")).strip()
    if not name:
        raise PlaybookError('Give the playbook a name (name = "...").')
    source_input = data.get("input") or {}
    column = str(source_input.get("column", "")).strip().lower()
    if not column:
        raise PlaybookError("Set input.column: the lead column the AI starts from.")
    sources = source_input.get("sources") or []
    if not isinstance(sources, list) or any(s not in SOURCES for s in sources):
        raise PlaybookError(f"input.sources can contain: {', '.join(SOURCES)}.")

    fields = []
    for key, spec in (data.get("fields") or {}).items():
        if not FIELD_NAME.fullmatch(key):
            raise PlaybookError(f'Field name "{key}": use lowercase letters, digits and _.')
        if not isinstance(spec, dict):
            raise PlaybookError(f"fields.{key} must be a table.")
        max_words = spec.get("max_words")
        if max_words is not None and (not isinstance(max_words, int) or max_words < 1):
            raise PlaybookError(f"fields.{key}.max_words must be a positive number.")
        alternatives = spec.get("alternatives", 0)
        if not isinstance(alternatives, int) or not 0 <= alternatives <= 8:
            raise PlaybookError(f"fields.{key}.alternatives must be between 0 and 8.")
        fallback = spec.get("fallback")
        fields.append(
            Field(
                name=key,
                describe=str(spec.get("describe", "")).strip(),
                max_words=max_words,
                required=bool(spec.get("required", fallback is None)),
                fallback=None if fallback is None else str(fallback),
                alternatives=alternatives,
            )
        )
    if not fields:
        raise PlaybookError("Add at least one field ([fields.hook] ...).")

    checks = data.get("checks") or {}
    banned = checks.get("banned") or []
    if not isinstance(banned, list) or not all(isinstance(b, str) for b in banned):
        raise PlaybookError("checks.banned must be a list of phrases.")
    return Playbook(
        name=name[:120],
        language=str(data.get("language", "auto")).strip() or "auto",
        input_column=column,
        sources=list(dict.fromkeys(sources)),
        fields=fields,
        instructions=instructions,
        checks=Checks(
            email_words=_range(checks.get("email_words"), "email_words"),
            subject_words=_range(checks.get("subject_words"), "subject_words"),
            banned=[b.strip() for b in banned if b.strip()],
            one_link=bool(checks.get("one_link", False)),
            numbers_in_data=bool(checks.get("numbers_in_data", False)),
        ),
    )


# --- writing --------------------------------------------------------------------------------


def _s(value: str) -> str:
    """A TOML basic string (JSON string escapes are valid TOML escapes)."""
    clean = "".join(ch for ch in value if ch >= " " or ch in "\n\t")
    return json.dumps(clean, ensure_ascii=False)


def dump(playbook: Playbook) -> str:
    lines = [
        "+++",
        f"name = {_s(playbook.name)}",
        f"language = {_s(playbook.language)}",
        "",
        "[input]",
        f"column = {_s(playbook.input_column)}",
        f"sources = [{', '.join(_s(s) for s in playbook.sources)}]",
    ]
    for f in playbook.fields:
        lines += ["", f"[fields.{f.name}]", f"describe = {_s(f.describe)}"]
        if f.max_words:
            lines.append(f"max_words = {int(f.max_words)}")
        if f.fallback is not None:
            lines.append(f"fallback = {_s(f.fallback)}")
        elif not f.required:
            lines.append("required = false")
        if f.alternatives:
            lines.append(f"alternatives = {int(f.alternatives)}")
    c = playbook.checks
    lines += ["", "[checks]"]
    if c.email_words:
        lines.append(f"email_words = [{c.email_words[0]}, {c.email_words[1]}]")
    if c.subject_words:
        lines.append(f"subject_words = [{c.subject_words[0]}, {c.subject_words[1]}]")
    lines.append(f"banned = [{', '.join(_s(b) for b in c.banned)}]")
    lines.append(f"one_link = {'true' if c.one_link else 'false'}")
    lines.append(f"numbers_in_data = {'true' if c.numbers_in_data else 'false'}")
    lines += ["+++", "", playbook.instructions.strip(), ""]
    return "\n".join(lines)


def fingerprint(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def ai_fields_used(text: str) -> set[str]:
    """Field names a template uses through {{ai_<name>}}."""
    return {name[3:] for name in variables_in(text) if name.startswith("ai_")}


# --- storage --------------------------------------------------------------------------------


def get(conn: sqlite3.Connection, playbook_id: int | None) -> sqlite3.Row | None:
    if not playbook_id:
        return None
    return conn.execute("SELECT * FROM playbooks WHERE id = ?", (playbook_id,)).fetchone()


def load(conn: sqlite3.Connection, playbook_id: int | None) -> Playbook | None:
    row = get(conn, playbook_id)
    if not row:
        return None
    try:
        return parse(row["source"])
    except PlaybookError:
        return None


def all_playbooks(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for row in conn.execute("SELECT * FROM playbooks ORDER BY builtin, name COLLATE NOCASE"):
        item = dict(row)
        try:
            item["parsed"], item["error"] = parse(row["source"]), None
        except PlaybookError as err:
            item["parsed"], item["error"] = None, str(err)
        item["campaigns"] = conn.execute(
            "SELECT COUNT(*) FROM campaigns WHERE playbook_id = ?", (row["id"],)
        ).fetchone()[0]
        item["approved"] = conn.execute(
            "SELECT COUNT(*) FROM ai_fields WHERE playbook_id = ? AND status = 'approved'", (row["id"],)
        ).fetchone()[0]
        rows.append(item)
    return rows


def save(
    conn: sqlite3.Connection, source: str, playbook_id: int | None = None
) -> tuple[int | None, str | None]:
    """Create or update from .playbook.md text. Returns (id, error). Built-in examples are read-only."""
    source = source.replace("\r\n", "\n")
    try:
        playbook = parse(source)
    except PlaybookError as err:
        return None, str(err)
    stamp = db.iso(db.now())
    if playbook_id is None:
        cur = conn.execute(
            "INSERT INTO playbooks (name, source, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (playbook.name, source, stamp, stamp),
        )
        return cur.lastrowid, None
    cur = conn.execute(
        "UPDATE playbooks SET name = ?, source = ?, updated_at = ? WHERE id = ? AND builtin = 0",
        (playbook.name, source, stamp, playbook_id),
    )
    if not cur.rowcount:
        return None, "Examples can't be edited; duplicate it first."
    # Drafts written under the old version are rewritten under the new one; approved ones stay.
    conn.execute(
        "UPDATE ai_fields SET status = 'pending', attempts = 0, error = NULL"
        " WHERE playbook_id = ? AND status IN ('needs_review', 'failed')",
        (playbook_id,),
    )
    return playbook_id, None


def delete(conn: sqlite3.Connection, playbook_id: int) -> str | None:
    used = conn.execute("SELECT name FROM campaigns WHERE playbook_id = ?", (playbook_id,)).fetchall()
    if used:
        return f'Used by campaign "{used[0]["name"]}". Pick another playbook there first.'
    conn.execute("DELETE FROM playbooks WHERE id = ? AND builtin = 0", (playbook_id,))
    return None


def sync_examples(conn: sqlite3.Connection) -> None:
    """Insert or refresh the example playbooks shipped in outreach_wise/playbooks/."""
    stamp = db.iso(db.now())
    for path in sorted(EXAMPLES_DIR.glob("*.playbook.md")):
        source = path.read_text(encoding="utf-8")
        playbook = parse(source)
        row = conn.execute(
            "SELECT id, source FROM playbooks WHERE builtin = 1 AND name = ?", (playbook.name,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO playbooks (name, source, builtin, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                (playbook.name, source, stamp, stamp),
            )
        elif row["source"] != source:
            conn.execute(
                "UPDATE playbooks SET source = ?, updated_at = ? WHERE id = ?", (source, stamp, row["id"])
            )
    conn.commit()
