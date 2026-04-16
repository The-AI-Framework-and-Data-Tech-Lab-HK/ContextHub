"""Unit tests for LLM runtime retry logic."""

from __future__ import annotations

from types import SimpleNamespace

from core.commit.llm_runtime import chat_completion_with_retry


class _FakeCompletions:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    def create(self, **kwargs):  # noqa: ANN003
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeChat:
    def __init__(self, outcomes):
        self.completions = _FakeCompletions(outcomes)


class _FakeClient:
    def __init__(self, outcomes):
        self.chat = _FakeChat(outcomes)


def _ok_response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    )


def test_chat_completion_with_retry_retries_then_succeeds(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("core.commit.llm_runtime.time.sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr("core.commit.llm_runtime.random.uniform", lambda a, b: 1.0)
    monkeypatch.setattr(
        "core.commit.llm_runtime.is_retryable_llm_error",
        lambda exc: isinstance(exc, ValueError),
    )
    monkeypatch.setattr("core.commit.llm_runtime._retry_after_seconds", lambda exc: 0.0)

    client = _FakeClient([ValueError("429-like"), _ok_response()])
    resp, retries = chat_completion_with_retry(
        client=client,
        provider_id="unit-provider",
        model="unit-model",
        temperature=0.0,
        messages=[{"role": "user", "content": "hello"}],
        max_concurrency=1,
        max_retries=3,
        base_backoff_seconds=0.2,
        max_backoff_seconds=1.0,
    )
    assert retries == 1
    assert resp.usage.total_tokens == 12
    assert sleeps == [0.2]


def test_chat_completion_with_retry_stops_on_non_retryable(monkeypatch) -> None:
    monkeypatch.setattr("core.commit.llm_runtime.is_retryable_llm_error", lambda exc: False)
    client = _FakeClient([RuntimeError("hard-fail")])
    try:
        chat_completion_with_retry(
            client=client,
            provider_id="unit-provider-2",
            model="unit-model",
            temperature=0.0,
            messages=[{"role": "user", "content": "hello"}],
            max_concurrency=1,
            max_retries=3,
            base_backoff_seconds=0.1,
            max_backoff_seconds=0.2,
        )
    except RuntimeError as exc:
        assert "hard-fail" in str(exc)
    else:
        raise AssertionError("expected RuntimeError to be raised")

