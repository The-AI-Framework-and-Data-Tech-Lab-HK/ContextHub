"""Auditable exact/bounded/unbounded cost accounting.

This module is intentionally separate from MEME behavior code.  It only
normalizes already-recorded usage and frozen request/price evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import math
from pathlib import Path
import json
from typing import Any


ACCOUNTING_SCHEMA_VERSION = "meme-cost-interval-v1"


def _money(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return number


def interval_record(
    *,
    known_usd: float,
    lower_usd: float | None = None,
    upper_usd: float | None,
    unknown_usage_attempts: Sequence[Mapping[str, Any]] = (),
    missing_token_types: Iterable[str] = (),
    upper_bound_sources: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build one canonical cost interval record.

    ``upper_usd=None`` means genuinely unbounded, never "unknown but probably
    small".  Known spend must be included in the lower bound.
    """

    known = _money(known_usd, "known_usd")
    lower = _money(known if lower_usd is None else lower_usd, "lower_usd")
    if lower + 1e-15 < known:
        raise ValueError("lower_usd cannot be below known_usd")
    upper = None if upper_usd is None else _money(upper_usd, "upper_usd")
    if upper is not None and upper + 1e-15 < lower:
        raise ValueError("upper_usd cannot be below lower_usd")
    unknown = [dict(row) for row in unknown_usage_attempts]
    missing = sorted({str(value) for value in missing_token_types})
    sources = [dict(row) for row in upper_bound_sources]
    exact = upper is not None and math.isclose(lower, upper, abs_tol=1e-15)
    bounded = upper is not None and not exact
    if unknown and exact:
        raise ValueError("unknown usage cannot be marked exact")
    if bounded and not sources:
        raise ValueError("bounded cost requires auditable upper-bound evidence")
    return {
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "known_usd": known,
        "lower_usd": lower,
        "upper_usd": upper,
        "cost_exact": exact,
        "cost_bounded": bounded,
        "cost_unbounded": upper is None,
        "unknown_usage_attempts": unknown,
        "unknown_usage_attempt_count": len(unknown),
        "missing_token_types": missing,
        "upper_bound_sources": sources,
    }


def exact_record(known_usd: float) -> dict[str, Any]:
    return interval_record(known_usd=known_usd, upper_usd=known_usd)


def bounded_unknown_completion(
    *,
    known_usd: float,
    max_completion_tokens: int | None,
    output_per_million: float | None,
    attempt: Mapping[str, Any],
    request_snapshot: Mapping[str, Any] | None,
    price_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bound a missing completion only from frozen request and price evidence."""

    if (
        max_completion_tokens is None
        or max_completion_tokens < 0
        or output_per_million is None
        or not math.isfinite(float(output_per_million))
        or float(output_per_million) < 0
        or request_snapshot is None
        or price_snapshot is None
    ):
        return interval_record(
            known_usd=known_usd,
            upper_usd=None,
            unknown_usage_attempts=[attempt],
            missing_token_types=["completion_tokens"],
        )
    delta = max_completion_tokens * float(output_per_million) / 1_000_000
    source = {
        "kind": "frozen_request_limit_x_frozen_output_price",
        "max_completion_tokens": int(max_completion_tokens),
        "output_per_million": float(output_per_million),
        "request_snapshot": dict(request_snapshot),
        "price_snapshot": dict(price_snapshot),
        "upper_delta_usd": delta,
    }
    return interval_record(
        known_usd=known_usd,
        upper_usd=known_usd + delta,
        unknown_usage_attempts=[attempt],
        missing_token_types=["completion_tokens"],
        upper_bound_sources=[source],
    )


def aggregate_intervals(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(row) for row in records]
    known = sum(_money(row["known_usd"], "known_usd") for row in rows)
    lower = sum(_money(row["lower_usd"], "lower_usd") for row in rows)
    bounded = all(row.get("upper_usd") is not None for row in rows)
    upper = (
        sum(_money(row["upper_usd"], "upper_usd") for row in rows)
        if bounded
        else None
    )
    unknown = [
        dict(attempt)
        for row in rows
        for attempt in row.get("unknown_usage_attempts") or ()
    ]
    sources = [
        dict(source)
        for row in rows
        for source in row.get("upper_bound_sources") or ()
    ]
    return interval_record(
        known_usd=known,
        lower_usd=lower,
        upper_usd=upper,
        unknown_usage_attempts=unknown,
        missing_token_types={
            str(token)
            for row in rows
            for token in row.get("missing_token_types") or ()
        },
        upper_bound_sources=sources,
    )


def interval_distribution(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        raise ValueError("cost distribution requires at least one record")
    rows = [dict(row) for row in records]

    def median(values: Sequence[float]) -> float:
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    lowers = [_money(row["lower_usd"], "lower_usd") for row in rows]
    uppers = (
        [_money(row["upper_usd"], "upper_usd") for row in rows]
        if all(row.get("upper_usd") is not None for row in rows)
        else None
    )
    total = aggregate_intervals(rows)
    count = len(rows)
    return {
        "count": count,
        "sum": {
            "lower_usd": total["lower_usd"],
            "upper_usd": total["upper_usd"],
        },
        "mean": {
            "lower_usd": sum(lowers) / count,
            "upper_usd": sum(uppers) / count if uppers is not None else None,
        },
        "median": {
            "lower_usd": median(lowers),
            "upper_usd": median(uppers) if uppers is not None else None,
        },
    }


def frozen_episode_interval(
    *,
    episode_id: str,
    cost_row: Mapping[str, Any],
    selector_rows: Sequence[Mapping[str, Any]],
    price_table: Mapping[str, Any],
) -> dict[str, Any]:
    """Upgrade legacy frozen-P1 evidence without episode-specific exceptions."""

    new = cost_row.get("new")
    if not isinstance(new, Mapping):
        raise ValueError(f"{episode_id}: frozen cost row lacks new accounting")
    known = _money(new.get("deployment"), "new.deployment")
    expected_unknown = int(new.get("cost_incomplete_attempts") or 0)
    attempts: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    upper_delta = 0.0
    for selector in selector_rows:
        for call_index, call in enumerate(selector.get("model_calls") or ()):
            # The frozen v3 producer defined >60s as proof that its fixed first
            # ReadTimeout occurred and the request was retried.
            if float(call.get("latency_seconds") or 0.0) <= 60:
                continue
            model = str(call.get("model") or "")
            usage = call.get("usage") or {}
            max_tokens = call.get("max_tokens")
            price = price_table.get(model)
            request = {
                "model": model,
                "prompt_sha256": call.get("prompt_sha256"),
                "prompt_bytes": len(str(call.get("prompt") or "").encode()),
                "max_completion_tokens": max_tokens,
            }
            attempt = {
                "episode_id": episode_id,
                "source": "frozen_v3_internal_retry",
                "selector_attempt_id": selector.get("attempt_id"),
                "logical_call_index": call_index,
                "known_prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": None,
                "missing_token_types": ["completion_tokens"],
                "request_snapshot": request,
                "price_snapshot": dict(price) if isinstance(price, Mapping) else None,
            }
            attempts.append(attempt)
            if (
                isinstance(max_tokens, int)
                and max_tokens >= 0
                and isinstance(price, Mapping)
                and price.get("output_per_million") is not None
            ):
                delta = max_tokens * float(price["output_per_million"]) / 1_000_000
                upper_delta += delta
                sources.append(
                    {
                        "kind": "frozen_request_limit_x_frozen_output_price",
                        "attempt_index": len(attempts) - 1,
                        "max_completion_tokens": max_tokens,
                        "output_per_million": float(price["output_per_million"]),
                        "request_snapshot": request,
                        "price_snapshot": dict(price),
                        "upper_delta_usd": delta,
                    }
                )
    if len(attempts) != expected_unknown:
        raise ValueError(
            f"{episode_id}: unknown-attempt evidence mismatch "
            f"{len(attempts)} != {expected_unknown}"
        )
    if not attempts:
        return exact_record(known)
    finite = len(sources) == len(attempts)
    return interval_record(
        known_usd=known,
        upper_usd=known + upper_delta if finite else None,
        unknown_usage_attempts=attempts,
        missing_token_types=["completion_tokens"],
        upper_bound_sources=sources if finite else (),
    )


def load_frozen_selector_rows(v3_dir: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    with (v3_dir / "case_success.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            result = row.get("result") or row
            grouped.setdefault(str(result["episode_id"]), []).append(result)
    return grouped
