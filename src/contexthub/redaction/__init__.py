"""Value-level text redaction primitives shared across ContextHub.

Pure string operations with no policy/ownership knowledge: given a set of raw
values, strip or block them from free text. Callers decide WHICH values are
unauthorized (that is policy-shaped and stays with the caller); this module only
implements HOW a known value set is removed from text.

Kept dependency-light (stdlib only) so any consumer — including the AgentLeak
benchmark process — can import it without pulling the heavier enforcement/db
stack.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

MASK_TOKEN = "[REDACTED]"
BLOCKED_TOKEN = "[BLOCKED]"


def _ordered_values(values: Iterable[Any]) -> list[str]:
    """Deduplicate, drop empties, sort longest-first.

    Longest-first replacement ensures a value that is a substring of another is
    not partially clobbered before the longer one is matched.
    """

    return sorted(
        {str(v) for v in values if v is not None and str(v)},
        key=len,
        reverse=True,
    )


def redact_values(text: str, values: Iterable[Any], *, mask: str = MASK_TOKEN) -> str:
    """Replace each value with ``mask`` in ``text`` (case-insensitive, longest-first)."""

    if not text:
        return text
    redacted = text
    for value in _ordered_values(values):
        redacted = re.sub(re.escape(value), mask, redacted, flags=re.IGNORECASE)
    return redacted


def block_if_present(
    text: str, values: Iterable[Any], *, token: str = BLOCKED_TOKEN
) -> str:
    """Return ``token`` if any value appears in ``text`` (case-insensitive); else ``text``."""

    if not text:
        return text
    lowered = text.lower()
    for value in values:
        if value is None:
            continue
        svalue = str(value)
        if svalue and svalue.lower() in lowered:
            return token
    return text


def redact_value_tree(value: Any, values: Iterable[Any], *, mask: str = MASK_TOKEN) -> Any:
    """Recursively redact values from strings inside str/dict/list structures."""

    if isinstance(value, str):
        return redact_values(value, values, mask=mask)
    if isinstance(value, dict):
        return {key: redact_value_tree(item, values, mask=mask) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value_tree(item, values, mask=mask) for item in value]
    return value


__all__ = [
    "MASK_TOKEN",
    "BLOCKED_TOKEN",
    "redact_values",
    "block_if_present",
    "redact_value_tree",
]
