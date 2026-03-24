"""Validate raw trajectory steps (sample_traj / API input shape)."""

from __future__ import annotations

from typing import Any

_VALID_ROLES = frozenset({"AIMessage", "ToolMessage"})


class TrajectoryValidationError(ValueError):
    """Raised when trajectory steps fail structural validation."""


def validate_raw_steps(steps: list[dict[str, Any]]) -> None:
    """
    - Non-empty list
    - Strictly increasing integer Step
    - meta.role in {AIMessage, ToolMessage}
    """
    if not steps:
        raise TrajectoryValidationError("trajectory must be non-empty")

    prev: int | None = None
    for i, step in enumerate(steps):
        if "Step" not in step:
            raise TrajectoryValidationError(f"step[{i}] missing Step")
        s = step["Step"]
        if not isinstance(s, int):
            raise TrajectoryValidationError(f"step[{i}] Step must be int, got {type(s)}")
        if prev is not None and s <= prev:
            raise TrajectoryValidationError(
                f"Step must be strictly increasing: {prev} then {s} at index {i}"
            )
        prev = s

        meta = step.get("meta") or {}
        role = meta.get("role")
        if role not in _VALID_ROLES:
            raise TrajectoryValidationError(
                f"step[{i}] meta.role must be one of {_VALID_ROLES}, got {role!r}"
            )


def collect_validation_issues(steps: list[dict[str, Any]]) -> list[str]:
    """Non-raising helper for softer validation / warnings."""
    try:
        validate_raw_steps(steps)
    except TrajectoryValidationError as e:
        return [str(e)]
    return []
