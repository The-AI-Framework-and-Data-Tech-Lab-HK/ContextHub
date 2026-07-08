"""Generic AgentLeak redaction baseline for AL-S2.

This module is intentionally policy-blind: it does not read AgentLeak
``allowed_set`` annotations, ContextHub ownership/provenance, AgentLeak
detectors, or an LLM judge. It only applies generic text patterns to channel
payloads so AL-S2 remains comparable with AL-S3 without borrowing AL-S3's field
policy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RedactionMatch:
    """A scrubbed summary of one generic redaction match."""

    pattern: str
    path: str
    replacement: str
    start: int
    end: int


@dataclass(frozen=True)
class RedactionResult:
    """Result returned by the policy-blind generic redactor."""

    content: Any
    matches: list[RedactionMatch] = field(default_factory=list)
    over_redaction_candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.matches)

    def summary(self) -> dict[str, Any]:
        patterns = sorted({match.pattern for match in self.matches})
        return {
            "redacted": self.redacted,
            "patterns": patterns,
            "count_by_pattern": {
                pattern: sum(1 for match in self.matches if match.pattern == pattern)
                for pattern in patterns
            },
            "over_redaction_candidates": list(self.over_redaction_candidates),
            "uses_allowed_set": False,
            "uses_online_llm_or_detector": False,
        }


@dataclass(frozen=True)
class RedactionRule:
    name: str
    regex: re.Pattern[str]
    replacement: str
    validator: Any = None


_CANARY_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:AGENTLEAK[_-]CANARY|CANARY|SECRET[_-]CANARY)"
    r"[A-Za-z0-9_.:=-]*",
    re.IGNORECASE,
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?1[\s.-]?)?"
    r"(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}\b"
)
_CREDIT_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule("canary", _CANARY_RE, "[REDACTED:CANARY]"),
    RedactionRule("ssn", _SSN_RE, "[REDACTED:SSN]"),
    RedactionRule("email", _EMAIL_RE, "[REDACTED:EMAIL]"),
    RedactionRule("credit_card", _CREDIT_CARD_RE, "[REDACTED:CREDIT_CARD]", "_luhn"),
    RedactionRule("phone", _PHONE_RE, "[REDACTED:PHONE]"),
)


class GenericRedactor:
    """Policy-blind recursive sanitizer for AL-S2 channel payloads."""

    def __init__(self, rules: tuple[RedactionRule, ...] = DEFAULT_RULES):
        self.rules = tuple(rules)

    def redact(self, content: Any) -> RedactionResult:
        matches: list[RedactionMatch] = []
        over_redaction_candidates: list[dict[str, Any]] = []
        redacted = self._redact_value(
            content,
            path="$",
            matches=matches,
            over_redaction_candidates=over_redaction_candidates,
        )
        return RedactionResult(
            content=redacted,
            matches=matches,
            over_redaction_candidates=over_redaction_candidates,
        )

    def _redact_value(
        self,
        value: Any,
        *,
        path: str,
        matches: list[RedactionMatch],
        over_redaction_candidates: list[dict[str, Any]],
    ) -> Any:
        if isinstance(value, str):
            return self._redact_text(
                value,
                path=path,
                matches=matches,
                over_redaction_candidates=over_redaction_candidates,
            )
        if isinstance(value, list):
            return [
                self._redact_value(
                    item,
                    path=f"{path}[{index}]",
                    matches=matches,
                    over_redaction_candidates=over_redaction_candidates,
                )
                for index, item in enumerate(value)
            ]
        if isinstance(value, tuple):
            return tuple(
                self._redact_value(
                    item,
                    path=f"{path}[{index}]",
                    matches=matches,
                    over_redaction_candidates=over_redaction_candidates,
                )
                for index, item in enumerate(value)
            )
        if isinstance(value, dict):
            return {
                key: self._redact_value(
                    item,
                    path=f"{path}.{key}",
                    matches=matches,
                    over_redaction_candidates=over_redaction_candidates,
                )
                for key, item in value.items()
            }
        return value

    def _redact_text(
        self,
        text: str,
        *,
        path: str,
        matches: list[RedactionMatch],
        over_redaction_candidates: list[dict[str, Any]],
    ) -> str:
        redacted = text
        for rule in self.rules:
            redacted = self._apply_rule(
                redacted,
                rule=rule,
                path=path,
                matches=matches,
                over_redaction_candidates=over_redaction_candidates,
            )
        return redacted

    def _apply_rule(
        self,
        text: str,
        *,
        rule: RedactionRule,
        path: str,
        matches: list[RedactionMatch],
        over_redaction_candidates: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        cursor = 0
        for match in rule.regex.finditer(text):
            raw = match.group(0)
            if rule.validator == "_luhn" and not _passes_luhn(raw):
                over_redaction_candidates.append(
                    {"pattern": rule.name, "path": path, "reason": "luhn_failed"}
                )
                continue
            parts.append(text[cursor : match.start()])
            parts.append(rule.replacement)
            matches.append(
                RedactionMatch(
                    pattern=rule.name,
                    path=path,
                    replacement=rule.replacement,
                    start=match.start(),
                    end=match.end(),
                )
            )
            cursor = match.end()
        if not parts:
            return text
        parts.append(text[cursor:])
        return "".join(parts)


def redact_generic(content: Any) -> RedactionResult:
    """Redact generic PII/canary patterns without reading policy annotations."""

    return GenericRedactor().redact(content)


def _passes_luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


__all__ = [
    "DEFAULT_RULES",
    "GenericRedactor",
    "RedactionMatch",
    "RedactionResult",
    "redact_generic",
]
