"""Turn a playbook + a lead into AI fields (and a guide into a playbook draft)."""

import base64
import json
import sqlite3
from datetime import datetime

from .. import db
from ..config import Config
from . import checks, keys, sources
from .playbooks import SOURCES, Checks, Field, Playbook, PlaybookError, dump, fingerprint, parse
from .providers import ProviderError, for_settings

MAX_ATTEMPTS = 3

FRAME = """You research one lead and write specific parts of a cold outreach email, following the
playbook below. Your text is inserted into an email template written by the user.

Rules that always apply:
- Use only facts present in the lead's columns or the research data. Never invent quotes,
  numbers, names, titles, dates or product features.
- If you are unsure about something, leave it out and tell the reviewer in notes_for_reviewer
  what to verify (for example: "the hook describes the video from its description only").
- evidence: one entry per factual claim you made, with where it came from (video title and
  date, About text, page section).
- If the lead should not be contacted under the playbook's rules, or there isn't enough real
  information to write something specific, set skip_reason and leave the fields empty.
  Otherwise skip_reason is an empty string.
- Each field must read naturally on its own inside the template. Don't add greetings,
  signatures or links to a field unless its description asks for it.
- Write in {language}.
"""


def _language(playbook: Playbook) -> str:
    if playbook.language in ("", "auto"):
        return "the language the lead's own content is in (their channel or website)"
    return playbook.language


def schema_for(playbook: Playbook) -> dict:
    field_props = {}
    for f in playbook.fields:
        description = f.describe or f.name
        if f.max_words:
            description += f" At most {f.max_words} words."
        if f.fallback is not None or not f.required:
            description += " Empty string if you can't write it from verified facts."
        field_props[f.name] = {"type": "string", "description": description}
    alt_props = {
        f.name: {
            "type": "array",
            "items": {"type": "string"},
            "description": f"{f.alternatives} other options for {f.name}, each from a different angle.",
        }
        for f in playbook.fields
        if f.alternatives
    }
    schema = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "properties": field_props,
                "required": list(field_props),
                "additionalProperties": False,
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"claim": {"type": "string"}, "source": {"type": "string"}},
                    "required": ["claim", "source"],
                    "additionalProperties": False,
                },
            },
            "notes_for_reviewer": {"type": "string"},
            "skip_reason": {"type": "string"},
        },
        "required": ["fields", "evidence", "notes_for_reviewer", "skip_reason"],
        "additionalProperties": False,
    }
    if alt_props:
        schema["properties"]["alternatives"] = {
            "type": "object",
            "properties": alt_props,
            "required": list(alt_props),
            "additionalProperties": False,
        }
        schema["required"].append("alternatives")
    return schema


def system_prompt(conn: sqlite3.Connection, playbook_id: int | None, playbook: Playbook) -> str:
    parts = [
        FRAME.format(language=_language(playbook)),
        f"# Playbook: {playbook.name}\n",
        playbook.instructions,
    ]
    parts.append("\n# Fields you write\n")
    for f in playbook.fields:
        parts.append(f"- {f.name}: {f.describe}" + (f" (max {f.max_words} words)" if f.max_words else ""))
    if playbook_id:
        examples = conn.execute(
            "SELECT field, before, after FROM ai_examples WHERE playbook_id = ? ORDER BY id DESC LIMIT 8",
            (playbook_id,),
        ).fetchall()
        if examples:
            parts.append("\n# How the reviewer corrected earlier drafts (follow these preferences)\n")
            for e in reversed(examples):
                parts.append(f"- {e['field']}\n  draft: {e['before']}\n  approved: {e['after']}")
    return "\n".join(parts)


def _lead_fields(lead: sqlite3.Row) -> dict:
    return {k: v for k, v in json.loads(lead["fields"]).items() if str(v).strip()}


def research(
    conn: sqlite3.Connection, cfg: Config, settings: dict, playbook: Playbook, lead: sqlite3.Row, client=None
):
    """(research data by source, error or None). An error means: retry later."""
    value = str(_lead_fields(lead).get(playbook.input_column, "")).strip()
    if not value:
        return None, f"no value in the {playbook.input_column} column"
    found = {}
    for source in playbook.sources:
        try:
            found[source] = sources.fetch(
                conn,
                lead["id"],
                source,
                value,
                youtube_key=keys.get(conn, cfg, "youtube_api_key"),
                stale_days=int(settings["ai_stale_days"]),
                client=client,
            )
        except sources.SourceError as err:
            return None, f"{SOURCES[source]}: {err}"
    return found, None


def write(
    conn: sqlite3.Connection,
    cfg: Config,
    settings: dict,
    playbook: Playbook,
    lead: sqlite3.Row,
    *,
    playbook_id: int | None = None,
    note: str | None = None,
    provider=None,
    client=None,
) -> dict:
    """Research + one model call. Never touches ai_fields; returns an outcome dict with
    status needs_review | skipped | retry | failed."""
    data, error = research(conn, cfg, settings, playbook, lead, client)
    if error and error.startswith("no value"):
        return {"status": "skipped", "skip_reason": f"Lead has {error}."}
    if error:
        return {"status": "retry", "error": error}
    lead_fields = _lead_fields(lead)
    lead_fields.pop("email", None)
    user = "## Lead\n" + json.dumps(lead_fields, ensure_ascii=False, indent=1)
    user += "\n\n## Research data (fetched " + db.iso(db.now())[:10] + ")\n"
    user += json.dumps(data, ensure_ascii=False)
    if note:
        user += f"\n\n## The reviewer asked for a new draft\n{note}"
    try:
        provider = provider or for_settings(conn, cfg, settings)
        result = provider.generate(system_prompt(conn, playbook_id, playbook), user, schema_for(playbook))
    except ProviderError as err:
        return {"status": "retry" if err.retryable else "failed", "error": str(err)}
    record_spend(conn, result.cost_usd)
    outcome = {
        "provider": provider.name,
        "model": result.model,
        "usage": result.usage,
        "cost_usd": result.cost_usd,
    }
    answer = result.data
    skip = str(answer.get("skip_reason") or "").strip()
    values, missing = {}, []
    for f in playbook.fields:
        value = str((answer.get("fields") or {}).get(f.name) or "").strip()
        if not value and f.fallback is not None:
            value = f.fallback
        if not value and f.required:
            missing.append(f.name)
        values[f.name] = value
    alternatives = {
        name: [str(a).strip() for a in options if str(a).strip()]
        for name, options in (answer.get("alternatives") or {}).items()
        if playbook.get_field(name)
    }
    known = checks.known_text(lead_fields, data, playbook.instructions)
    outcome.update(
        vals=values,
        alternatives=alternatives,
        evidence=answer.get("evidence") or [],
        notes=str(answer.get("notes_for_reviewer") or "").strip(),
        checks=checks.field_checks(playbook, values, known),
    )
    if skip or missing:
        outcome.update(status="skipped", skip_reason=skip or f"The AI couldn't write {', '.join(missing)}.")
    else:
        outcome["status"] = "needs_review"
    return outcome


def run_one(
    conn: sqlite3.Connection, cfg: Config, settings: dict, ai_id: int, provider=None, client=None
) -> str:
    """Generate one pending ai_fields row. Returns its new status."""
    row = conn.execute("SELECT * FROM ai_fields WHERE id = ?", (ai_id,)).fetchone()
    pb_row = conn.execute("SELECT * FROM playbooks WHERE id = ?", (row["playbook_id"],)).fetchone()
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (row["lead_id"],)).fetchone()
    stamp = db.iso(db.now())
    try:
        playbook = parse(pb_row["source"])
    except PlaybookError as err:
        conn.execute(
            "UPDATE ai_fields SET status = 'failed', error = ? WHERE id = ?", (f"Playbook: {err}", ai_id)
        )
        conn.commit()
        return "failed"
    conn.execute("UPDATE ai_fields SET status = 'researching' WHERE id = ?", (ai_id,))
    conn.commit()

    outcome = write(
        conn, cfg, settings, playbook, lead,
        playbook_id=pb_row["id"], note=row["regenerate_note"], provider=provider, client=client,
    )  # fmt: skip
    status = outcome["status"]
    attempts = row["attempts"] + 1
    if status == "retry":
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        conn.execute(
            "UPDATE ai_fields SET status = ?, attempts = ?, error = ? WHERE id = ?",
            (status, attempts, outcome["error"], ai_id),
        )
    elif status == "failed" and "vals" not in outcome:
        conn.execute(
            "UPDATE ai_fields SET status = 'failed', attempts = ?, error = ? WHERE id = ?",
            (attempts, outcome["error"], ai_id),
        )
    else:
        conn.execute(
            """
            UPDATE ai_fields SET status = ?, attempts = ?, error = NULL, vals = ?, original_vals = NULL,
                alternatives = ?, evidence = ?, notes = ?, skip_reason = ?, checks = ?, regenerate_note = NULL,
                playbook_hash = ?, provider = ?, model = ?, usage = ?, cost_usd = cost_usd + ?,
                generated_at = ?, reviewed_at = NULL
            WHERE id = ?
            """,
            (
                status,
                attempts,
                json.dumps(outcome.get("vals", {}), ensure_ascii=False),
                json.dumps(outcome.get("alternatives", {}), ensure_ascii=False),
                json.dumps(outcome.get("evidence", []), ensure_ascii=False),
                outcome.get("notes"),
                outcome.get("skip_reason"),
                json.dumps(outcome.get("checks", []), ensure_ascii=False),
                fingerprint(pb_row["source"]),
                outcome.get("provider"),
                outcome.get("model"),
                json.dumps(outcome.get("usage", {})),
                outcome.get("cost_usd", 0),
                stamp,
                ai_id,
            ),
        )
    level = "warning" if status in ("failed", "skipped") else "info"
    detail = outcome.get("error") or outcome.get("skip_reason") or "ready for review"
    db.log_event(
        conn,
        "ai",
        f"AI fields for {lead['email']} ({pb_row['name']}): {status}, {detail}",
        level=level,
        lead_id=lead["id"],
    )
    conn.commit()
    return status


# --- usage & budget -------------------------------------------------------------------------


def _usage(conn: sqlite3.Connection) -> dict:
    today = db.iso(db.now())[:10]
    usage = db.get_kv(conn, "ai_usage", {}) or {}
    if usage.get("month") != today[:7]:
        usage.update(month=today[:7], spend=0.0)
    if usage.get("day") != today:
        usage.update(day=today, count=0)
    return usage


def record_spend(conn: sqlite3.Connection, amount: float, count: int = 0) -> None:
    usage = _usage(conn)
    usage["spend"] = round(usage.get("spend", 0) + amount, 6)
    usage["count"] = usage.get("count", 0) + count
    db.set_kv(conn, "ai_usage", usage)


def usage_today(conn: sqlite3.Connection) -> dict:
    return _usage(conn)


def over_limits(conn: sqlite3.Connection, settings: dict) -> str | None:
    usage = _usage(conn)
    if usage.get("count", 0) >= int(settings["ai_daily_limit"]):
        return "Daily research limit reached."
    if usage.get("spend", 0) >= float(settings["ai_monthly_budget_usd"]):
        return "Monthly AI budget reached."
    return None


# --- drafting a playbook from a guide ---------------------------------------------------------

DRAFT_SYSTEM = """You turn a team's cold outreach guide into a playbook for an email tool.

The tool keeps the fixed parts of the email (who we are, the offer, the call to action) in a
template the user writes. The AI only writes the short personal parts per lead, as named
fields (for example: greeting, hook, why_you, subject). Read the guide and produce:

- name: a short name for the playbook.
- input_column: the lead column the research starts from. Choose from the columns given.
- sources: which research sources to use: "youtube_channel" for YouTube channel links,
  "web_page" for websites. Only what the guide needs and the tool supports.
- language: the language emails are written in ("auto" = the lead's own language).
- fields: 2 to 5 personal parts the guide asks for, each with a lowercase name, a precise
  description of what to write (in the guide's own terms), max_words, an optional fallback
  (text used when it can't be written from facts, e.g. a neutral greeting; empty = none) and
  alternatives (how many extra options to offer; 3 for a subject line, otherwise 0).
- checks: word ranges for the whole email and the subject, banned phrases the guide forbids,
  whether only one link is allowed, and whether every number must come from the data.
- instructions: the guide rewritten as clear Markdown instructions for the AI writer: who we
  are and product facts, the rules, how to choose what to write about, what never to do,
  when to skip a lead, and the example emails. Keep every concrete rule and example. Leave out
  steps a model can't do here (watching videos, reading transcripts, browsing), and steps
  for writing the fixed parts that live in the template.
"""


def _draft_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "input_column": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string", "enum": list(SOURCES)}},
            "language": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "describe": {"type": "string"},
                        "max_words": {"type": "integer"},
                        "fallback": {"type": "string"},
                        "alternatives": {"type": "integer"},
                    },
                    "required": ["name", "describe", "max_words", "fallback", "alternatives"],
                    "additionalProperties": False,
                },
            },
            "checks": {
                "type": "object",
                "properties": {
                    "email_words_min": {"type": "integer"},
                    "email_words_max": {"type": "integer"},
                    "subject_words_min": {"type": "integer"},
                    "subject_words_max": {"type": "integer"},
                    "banned": {"type": "array", "items": {"type": "string"}},
                    "one_link": {"type": "boolean"},
                    "numbers_in_data": {"type": "boolean"},
                },
                "required": [
                    "email_words_min", "email_words_max", "subject_words_min", "subject_words_max",
                    "banned", "one_link", "numbers_in_data",
                ],
                "additionalProperties": False,
            },
            "instructions": {"type": "string"},
        },
        "required": ["name", "input_column", "sources", "language", "fields", "checks", "instructions"],
        "additionalProperties": False,
    }  # fmt: skip


def _slug(name: str) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
    while "__" in clean:
        clean = clean.replace("__", "_")
    return (clean or "field")[:40] if clean[:1].isalpha() else f"f_{clean}"[:40]


def _range(low, high) -> tuple[int, int] | None:
    try:
        low, high = int(low), int(high)
    except (TypeError, ValueError):
        return None
    return (low, high) if 0 < low <= high else None


def draft_playbook(
    conn: sqlite3.Connection,
    cfg: Config,
    settings: dict,
    *,
    guide_text: str = "",
    pdf: bytes | None = None,
    columns: list[str],
    provider=None,
) -> str:
    """One model call: a guide (text or PDF) → .playbook.md text. Raises ProviderError."""
    provider = provider or for_settings(conn, cfg, settings)
    intro = (
        f"Lead columns available: {', '.join(columns) or '(none yet)'}\n"
        f"Today: {datetime.now().date().isoformat()}\n\nThe guide:\n"
    )
    if pdf is not None:
        if provider.name != "anthropic":
            raise ProviderError(
                "PDF guides need the Anthropic API provider. Paste the guide as text instead."
            )
        user = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(pdf).decode(),
                },
            },
            {"type": "text", "text": intro + "(attached PDF)"},
        ]
    else:
        user = intro + guide_text
    result = provider.generate(DRAFT_SYSTEM, user, _draft_schema(), max_tokens=32000)
    record_spend(conn, result.cost_usd)
    conn.commit()
    data = result.data
    fields, seen = [], set()
    for spec in data.get("fields") or []:
        name = _slug(str(spec.get("name", "")))
        if name in seen:
            continue
        seen.add(name)
        fallback = str(spec.get("fallback") or "").strip()
        fields.append(
            Field(
                name=name,
                describe=str(spec.get("describe", "")).strip(),
                max_words=int(spec["max_words"]) if int(spec.get("max_words") or 0) > 0 else None,
                required=not fallback,
                fallback=fallback or None,
                alternatives=max(0, min(int(spec.get("alternatives") or 0), 8)),
            )
        )
    c = data.get("checks") or {}
    column = str(data.get("input_column") or "").strip().lower()
    playbook = Playbook(
        name=str(data.get("name") or "New playbook").strip()[:120],
        language=str(data.get("language") or "auto").strip() or "auto",
        input_column=column if column in columns or not columns else columns[0],
        sources=[s for s in data.get("sources") or [] if s in SOURCES],
        fields=fields or [Field(name="hook", describe="One specific, verified observation about the lead.")],
        instructions=str(data.get("instructions") or "").strip(),
        checks=Checks(
            email_words=_range(c.get("email_words_min"), c.get("email_words_max")),
            subject_words=_range(c.get("subject_words_min"), c.get("subject_words_max")),
            banned=[str(b).strip() for b in c.get("banned") or [] if str(b).strip()],
            one_link=bool(c.get("one_link")),
            numbers_in_data=bool(c.get("numbers_in_data")),
        ),
    )
    text = dump(playbook)
    parse(text)  # always round-trips; raises if something is off
    return text
