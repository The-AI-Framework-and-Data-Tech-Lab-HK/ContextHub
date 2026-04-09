"""MaskingService: best-effort keyword-level content masking.

Performs regex-level keyword replacement on natural-language text.
Not cryptographic isolation — designed to reduce sensitive-information
exposure surface in Agent context windows.
"""

from __future__ import annotations

import re
from typing import Any

_MASKED = "[MASKED]"


class MaskingService:

    def apply_masks(self, content: str | None, field_masks: list[str]) -> str | None:
        """Replace occurrences of *field_masks* keywords with ``[MASKED]``.

        Args:
            content: Text to mask.  ``None`` passes through unchanged.
            field_masks: Keywords to mask (e.g. ``["salary", "ssn"]``).

        Returns:
            Masked text, or the original value when *content* is falsy
            or *field_masks* is empty.
        """
        if not content or not field_masks:
            return content
        for keyword in field_masks:
            content = self._mask_keyword(content, keyword)
        return content

    def apply_masks_json(
        self,
        data: list[dict[str, Any]] | None,
        field_masks: list[str],
    ) -> list[dict[str, Any]] | None:
        """Key-matching masking for tabular JSON (e.g. ``sample_data``).

        For each row-dict, if a key case-insensitively matches any
        *field_masks* keyword the value is replaced with ``[MASKED]``.
        Non-dict items and non-matching keys are left untouched.

        This intentionally does NOT recurse into nested structures or
        apply text-level ``re.sub`` on values.  See ADR-backlog entry
        "Structured-field masking strategy" for rationale.
        """
        if not data or not field_masks:
            return data
        lower_masks = {kw.lower() for kw in field_masks}
        return [
            self._mask_row(row, lower_masks) if isinstance(row, dict) else row
            for row in data
        ]

    @staticmethod
    def _mask_row(row: dict[str, Any], lower_masks: set[str]) -> dict[str, Any]:
        return {
            k: (_MASKED if k.lower() in lower_masks else v)
            for k, v in row.items()
        }

    @staticmethod
    def _mask_keyword(content: str, keyword: str) -> str:
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        return pattern.sub(_MASKED, content)
