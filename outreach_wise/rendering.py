"""Email template rendering.

Syntax: {{variable}} or {{variable|fallback}}. Variables are the lead's sheet columns
(normalized: "First Name" -> first_name) plus `email` and `unsubscribe_url`.
A variable that is empty and has no fallback raises MissingVariable, so an email
is never sent with a literal "{{first_name}}" or a blank greeting.
"""

import re

VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*(?:\|([^}]*))?\}\}")


class MissingVariable(Exception):
    def __init__(self, name: str):
        super().__init__(f"missing value for {{{{{name}}}}}")
        self.name = name


def render(text: str, variables: dict) -> str:
    def substitute(match: re.Match) -> str:
        name, fallback = match.group(1).lower(), match.group(2)
        value = str(variables.get(name) or "").strip()
        if value:
            return value
        if fallback is not None:
            return fallback.strip()
        raise MissingVariable(name)

    return VARIABLE.sub(substitute, text)


def variables_in(text: str) -> set[str]:
    return {m.group(1).lower() for m in VARIABLE.finditer(text)}


def required_variables(text: str) -> set[str]:
    """Variables used without a fallback."""
    return {m.group(1).lower() for m in VARIABLE.finditer(text) if m.group(2) is None}


def single_line(text: str) -> str:
    """Collapse whitespace; used for subjects so lead data can't inject headers."""
    return " ".join(text.split())
