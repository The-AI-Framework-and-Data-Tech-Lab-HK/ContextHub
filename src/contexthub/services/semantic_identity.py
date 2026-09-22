"""Canonical semantic identity shared by Python and PostgreSQL.

Each nullable content level is normalized independently: ``None`` becomes the
empty string and every non-empty Unicode whitespace run becomes one ASCII
space.  The three normalized levels are joined with U+001F, UTF-8 encoded, and
SHA-256 hashed.
"""

from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"[ \t\n\r\f\v]+")


def canonicalize_semantic_part(value: str | None) -> str:
    """Collapse ASCII whitespace exactly once and strip ASCII spaces."""

    return _WHITESPACE.sub(" ", value or "").strip(" ")


def semantic_identity(*contents: str | None) -> str:
    """Return the canonical identity for the supplied content levels."""

    normalized = "\x1f".join(canonicalize_semantic_part(part) for part in contents)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


SEMANTIC_IDENTITY_TEST_VECTORS = (
    ((None, None, None), semantic_identity(None, None, None)),
    (("a  b", "c", None), semantic_identity("a b", "c", "")),
    (("a\nb", "c\td", None), semantic_identity("a b", "c d", "")),
)
