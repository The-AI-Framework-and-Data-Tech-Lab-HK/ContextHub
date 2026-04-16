"""Token-aware micro-batch planning for commit prepare stage."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from core.commit.service import CommitCommand

_MODEL_CONTEXT_LOCK = threading.Lock()
_MODEL_CONTEXT_CACHE: dict[tuple[str, str], int] = {}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def estimate_text_tokens(text: str) -> int:
    # Conservative tokenizer approximation:
    # - ascii-like text: ~4 chars/token
    # - non-ascii text: ~1.8 chars/token
    if not text:
        return 1
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    estimate = (ascii_chars / 4.0) + (non_ascii / 1.8)
    return max(1, int(math.ceil(estimate)))


def estimate_prepare_tokens(command: CommitCommand) -> int:
    # Approximate total prompt load for dataflow/reasoning/summary calls.
    payload = {
        "session_id": command.session_id,
        "task_id": command.task_id,
        "trajectory": command.trajectory,
        "labels": command.labels,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    core_tokens = estimate_text_tokens(text)
    fixed_overhead = 320
    expected_completion = 380
    # Commit prepare stage issues ~3 chat calls (dataflow + reasoning + summary).
    return (core_tokens + fixed_overhead + expected_completion) * 3


def _extract_context_tokens(model_obj: Any) -> int | None:
    if model_obj is None:
        return None
    candidate_fields = (
        "max_context_tokens",
        "context_window",
        "input_token_limit",
        "max_input_tokens",
        "token_limit",
    )
    for name in candidate_fields:
        value = getattr(model_obj, name, None)
        if isinstance(value, int) and value > 0:
            return value

    # Some providers return pydantic-like objects.
    dump = None
    if hasattr(model_obj, "model_dump"):
        try:
            dump = model_obj.model_dump()
        except Exception:
            dump = None
    if isinstance(dump, dict):
        for name in candidate_fields:
            value = dump.get(name)
            if isinstance(value, int) and value > 0:
                return value
    return None


def resolve_max_context_tokens(
    *,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    fallback: int,
) -> int:
    m = str(model or "").strip()
    key = ((base_url or "").strip().lower(), m.lower())
    if not m:
        return max(1, int(fallback))
    with _MODEL_CONTEXT_LOCK:
        cached = _MODEL_CONTEXT_CACHE.get(key)
    if cached:
        return cached
    if not api_key:
        return max(1, int(fallback))

    value = None
    try:
        client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=5.0)
        model_obj = client.models.retrieve(m)
        value = _extract_context_tokens(model_obj)
    except Exception:
        value = None

    resolved = max(1, int(value if isinstance(value, int) and value > 0 else fallback))
    with _MODEL_CONTEXT_LOCK:
        _MODEL_CONTEXT_CACHE[key] = resolved
    return resolved


@dataclass
class PlannedMicroBatch:
    indices: list[int]
    estimated_tokens: int


def plan_prepare_micro_batches(
    commands: list[CommitCommand],
    *,
    llm_token_usage_ratio: float,
    max_items_per_batch: int,
    max_context_tokens_fallback: int,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
) -> tuple[list[PlannedMicroBatch], int, int]:
    if not commands:
        return [], 0, 0

    ratio = _clamp(float(llm_token_usage_ratio), 0.1, 0.95)
    max_context = resolve_max_context_tokens(
        model=model,
        api_key=api_key,
        base_url=base_url,
        fallback=max_context_tokens_fallback,
    )
    token_budget = max(1, int(math.floor(max_context * ratio)))
    item_cap = max(1, int(max_items_per_batch))

    batches: list[PlannedMicroBatch] = []
    cur_indices: list[int] = []
    cur_tokens = 0

    for idx, command in enumerate(commands):
        est = estimate_prepare_tokens(command)
        if not cur_indices:
            cur_indices = [idx]
            cur_tokens = est
            continue

        exceeds_item_cap = len(cur_indices) >= item_cap
        exceeds_budget = (cur_tokens + est) > token_budget
        if exceeds_item_cap or exceeds_budget:
            batches.append(PlannedMicroBatch(indices=list(cur_indices), estimated_tokens=cur_tokens))
            cur_indices = [idx]
            cur_tokens = est
            continue

        cur_indices.append(idx)
        cur_tokens += est

    if cur_indices:
        batches.append(PlannedMicroBatch(indices=list(cur_indices), estimated_tokens=cur_tokens))
    return batches, token_budget, max_context

