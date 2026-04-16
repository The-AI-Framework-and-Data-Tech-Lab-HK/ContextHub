"""Runtime helpers for provider-aware LLM calls."""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

_SEMAPHORE_LOCK = threading.Lock()
_PROVIDER_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}


def provider_key(*, base_url: str | None, model: str) -> str:
    host = (base_url or "openai-default").strip().lower()
    return f"{host}::{model.strip().lower()}"


def _get_provider_semaphore(key: str, max_concurrency: int) -> threading.BoundedSemaphore:
    with _SEMAPHORE_LOCK:
        sem = _PROVIDER_SEMAPHORES.get(key)
        if sem is None:
            sem = threading.BoundedSemaphore(value=max(1, int(max_concurrency)))
            _PROVIDER_SEMAPHORES[key] = sem
        return sem


def _retry_after_seconds(exc: Exception) -> float | None:
    if not isinstance(exc, APIStatusError):
        return None
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except Exception:
        return None
    return max(0.0, value)


def is_retryable_llm_error(exc: Exception) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        code = int(getattr(exc, "status_code", 0) or 0)
        return code in {408, 409, 429, 500, 502, 503, 504}
    return False


def default_retry_config() -> dict[str, float | int]:
    return {
        "max_concurrency": int(os.getenv("AMC_LLM_MAX_CONCURRENCY", "4")),
        "max_retries": int(os.getenv("AMC_LLM_MAX_RETRIES", "5")),
        "base_backoff_seconds": float(os.getenv("AMC_LLM_BACKOFF_BASE_SECONDS", "1.0")),
        "max_backoff_seconds": float(os.getenv("AMC_LLM_BACKOFF_MAX_SECONDS", "15.0")),
    }


def chat_completion_with_retry(
    *,
    client: Any,
    provider_id: str,
    model: str,
    temperature: float,
    messages: list[dict[str, Any]],
    max_concurrency: int | None = None,
    max_retries: int | None = None,
    base_backoff_seconds: float | None = None,
    max_backoff_seconds: float | None = None,
) -> tuple[Any, int]:
    cfg = default_retry_config()
    concurrency = int(max_concurrency or cfg["max_concurrency"])
    retries = int(max_retries if max_retries is not None else cfg["max_retries"])
    backoff_base = float(
        base_backoff_seconds if base_backoff_seconds is not None else cfg["base_backoff_seconds"]
    )
    backoff_max = float(
        max_backoff_seconds if max_backoff_seconds is not None else cfg["max_backoff_seconds"]
    )

    sem = _get_provider_semaphore(provider_id, concurrency)
    attempt = 0
    while True:
        try:
            with sem:
                response = client.chat.completions.create(
                    model=model,
                    temperature=temperature,
                    messages=messages,
                )
            return response, attempt
        except Exception as exc:
            if (not is_retryable_llm_error(exc)) or attempt >= retries:
                raise
            retry_after = _retry_after_seconds(exc) or 0.0
            exp_delay = min(backoff_max, backoff_base * (2**attempt))
            jitter = random.uniform(0.85, 1.15)
            sleep_seconds = max(retry_after, exp_delay * jitter)
            time.sleep(sleep_seconds)
            attempt += 1

