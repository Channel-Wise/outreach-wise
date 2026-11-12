"""Automatic checks on what the AI wrote. They flag problems for the reviewer; they never block."""

import json
import re

from .playbooks import Playbook

WORD = re.compile(r"\w+(?:['’\-]\w+)*")
NUMBER = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)*)\s*([KkMmBb])?(?![\w])")
LINK = re.compile(
    r"https?://\S+|\bwww\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|ai|co|app|dev|tv|me)\b(?:/\S*)?"
)


def words(text: str) -> int:
    return len(WORD.findall(text))


def _ok(text: str) -> dict:
    return {"ok": True, "text": text}


def _warn(text: str) -> dict:
    return {"ok": False, "text": text}


def _numbers(text: str) -> list[tuple[str, float]]:
    found = []
    for raw, suffix in NUMBER.findall(text):
        digits = raw.replace(",", ".") if raw.count(",") == 1 and len(raw.split(",")[1]) <= 2 else raw
        digits = digits.replace(",", "")
        if digits.count(".") > 1 or (
            digits.count(".") == 1 and len(digits.split(".")[1]) == 3 and not suffix
        ):
            digits = digits.replace(".", "")  # 48.000 (Turkish thousands)
        try:
            value = float(digits)
        except ValueError:
            continue
        value *= {"k": 1e3, "b": 1e3, "m": 1e6}.get(suffix.lower(), 1) if suffix else 1
        found.append((raw + suffix, value))
    return found


def _banned(playbook: Playbook, text: str) -> list[str]:
    lowered = text.casefold()
    return [phrase for phrase in playbook.checks.banned if phrase.casefold() in lowered]


def field_checks(playbook: Playbook, values: dict, known_text: str) -> list[dict]:
    """Checks on the fields alone. known_text: the research data + lead columns + instructions."""
    results = []
    for f in playbook.fields:
        value = values.get(f.name, "")
        if f.max_words and value:
            count = words(value)
            results.append(
                _ok(f"ai_{f.name}: {count} words")
                if count <= f.max_words
                else _warn(f"ai_{f.name}: {count} words (max {f.max_words})")
            )
    joined = "\n".join(str(v) for v in values.values())
    hits = _banned(playbook, joined)
    if playbook.checks.banned:
        results.append(_warn(f"Banned phrase: {', '.join(hits)}") if hits else _ok("No banned phrases"))
    if playbook.checks.numbers_in_data:
        known = [value for _, value in _numbers(known_text)]
        unknown = [
            raw
            for raw, value in _numbers(joined)
            if value >= 10 and not any(abs(k - value) <= max(value * 0.06, 1) for k in known)
        ]
        results.append(
            _warn(f"Numbers not found in the data: {', '.join(unknown)}")
            if unknown
            else _ok("Numbers match the fetched data")
        )
    return results


def email_checks(playbook: Playbook, subject: str, body: str, first: bool = True) -> list[dict]:
    """Checks on the finished email (template + fields), shown in Review. The playbook's
    email length is for the first email; follow-ups are meant to be shorter."""
    results = []
    c = playbook.checks
    if c.email_words and first:
        count = words(body)
        low, high = c.email_words
        results.append(
            _ok(f"Email {count} words ({low}–{high})")
            if low <= count <= high
            else _warn(f"Email {count} words (expected {low}–{high})")
        )
    if c.subject_words and subject:
        count = words(subject)
        low, high = c.subject_words
        results.append(
            _ok(f"Subject {count} words")
            if low <= count <= high
            else _warn(f"Subject {count} words (expected {low}–{high})")
        )
    hits = _banned(playbook, f"{subject}\n{body}")
    if hits:
        results.append(_warn(f"Banned phrase in the email: {', '.join(hits)}"))
    if c.one_link:
        links = LINK.findall(body)
        results.append(_ok("One link") if len(links) <= 1 else _warn(f"{len(links)} links (expected one)"))
    return results


def known_text(lead_fields: dict, research: dict, instructions: str) -> str:
    return "\n".join(
        [json.dumps(lead_fields, ensure_ascii=False), json.dumps(research, ensure_ascii=False), instructions]
    )
