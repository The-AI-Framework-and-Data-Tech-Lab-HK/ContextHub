"""LLM utility-judge tests (Phase 5 formal-matrix step 2).

The judge is a post-hoc evaluation tool, opt-in and off by default. These tests
use a fake client/judge and NEVER make a real API call. They verify: response
parsing and graceful degradation in the judge module; and opt-in wiring in the
offline evaluator (judge runs only on C1, only when enabled, aggregated into
metrics, and never called when disabled).
"""
import asyncio
import json

import pytest

from integrations.agentleak.utility_judge import UtilityJudge, _parse_judgement
from integrations.agentleak.run_eval import run_offline_real_traces
from tests.test_agentleak_run_eval import _real_trace_fixture


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _FakeResponse(self._content)


class _FakeClient:
    """Minimal OpenAI-compatible stub returning a fixed JSON body."""

    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(content)})()


# --- judge module: parsing & degradation -----------------------------------


def test_parse_judgement_reads_success_and_score():
    parsed = _parse_judgement('{"success": true, "score": 0.9, "explanation": "ok"}')
    assert parsed == {"judged": True, "success": True, "score": 0.9}


def test_parse_judgement_derives_missing_field():
    assert _parse_judgement('{"success": false}') == {
        "judged": True,
        "success": False,
        "score": 0.0,
    }
    assert _parse_judgement('{"score": 0.8}') == {
        "judged": True,
        "score": 0.8,
        "success": True,
    }


def test_parse_judgement_handles_malformed_response():
    assert _parse_judgement("not json")["judged"] is False
    assert _parse_judgement("[1, 2, 3]")["judged"] is False
    assert _parse_judgement("{}")["judged"] is False


def test_judge_uses_injected_client_and_does_not_persist_explanation():
    client = _FakeClient('{"success": true, "score": 1.0, "explanation": "leak: secret"}')
    judge = UtilityJudge(client=client)

    result = judge.judge_completion("do the task", "the answer")

    assert result == {"judged": True, "success": True, "score": 1.0}
    assert "explanation" not in result  # explanation never returned to callers


def test_judge_degrades_without_client():
    # No injected client and openai/key unavailable in the test venv.
    judge = UtilityJudge(client=None, provider_label="does-not-exist")
    result = judge.judge_completion("task", "output")
    assert result["judged"] is False
    assert "skipped_reason" in result


def test_judge_reports_call_failure_class_only():
    class _BoomCompletions:
        def create(self, **kwargs):
            raise RuntimeError("boom with key sk-secret")

    client = type("C", (), {"chat": type("Chat", (), {"completions": _BoomCompletions()})()})()
    judge = UtilityJudge(client=client)

    result = judge.judge_completion("task", "output")

    assert result["judged"] is False
    assert result["skipped_reason"].startswith("call_failed:")
    assert "sk-secret" not in json.dumps(result)


# --- run_eval opt-in wiring -------------------------------------------------


class _RecordingJudge:
    def __init__(self, success=True, score=0.75):
        self._success = success
        self._score = score
        self.requests = []

    def judge_completion(self, request, output):
        self.requests.append((request, output))
        return {"judged": True, "success": self._success, "score": self._score}


class _RaisingJudge:
    def judge_completion(self, request, output):  # pragma: no cover - must not run
        raise AssertionError("judge must not be called when judge_utility is False")


def _write_trace(tmp_path):
    trace_file = tmp_path / "trace_real_fixture_1.json"
    trace_file.write_text(json.dumps(_real_trace_fixture()), encoding="utf-8")
    return trace_file


def test_judge_disabled_by_default_is_not_called(tmp_path):
    trace_file = _write_trace(tmp_path)
    result = asyncio.run(
        run_offline_real_traces(
            run_id="judge_off",
            trace_paths=[trace_file],
            runs_dir=tmp_path / "runs",
            channels=("C1", "C2", "C5"),
            append_to_registry=False,
            judge=_RaisingJudge(),  # provided but must stay dormant
        )
    )
    s0 = result["metrics"]["systems"]["AL-S0"]
    assert s0["llm_judge_utility"]["score"] is None
    assert s0["llm_judge_utility"]["skipped_reason"] == "llm_judge_not_run"
    assert result["metrics"]["llm_judge_utility_enabled"] is False


def test_judge_enabled_aggregates_only_c1(tmp_path):
    trace_file = _write_trace(tmp_path)
    judge = _RecordingJudge(success=True, score=0.75)
    result = asyncio.run(
        run_offline_real_traces(
            run_id="judge_on",
            trace_paths=[trace_file],
            runs_dir=tmp_path / "runs",
            systems=("AL-S0", "AL-S3-repair"),
            channels=("C1", "C2", "C5"),
            append_to_registry=False,
            judge_utility=True,
            judge=judge,
        )
    )

    assert result["metrics"]["llm_judge_utility_enabled"] is True
    s0 = result["metrics"]["systems"]["AL-S0"]
    assert s0["llm_judge_utility"]["score"] == 0.75
    assert s0["llm_judge_utility"]["success_rate"] == 1.0
    # One C1 event per system per trace -> exactly one judged item.
    assert s0["llm_judge_utility"]["n"] == 1
    # The judge only ever saw the request as task and a C1 output as answer.
    assert all(req == "Help coordinate the patient handoff for Jane Doe." for req, _ in judge.requests)

    # No raw sensitive value reaches persisted artifacts even with judging on.
    from pathlib import Path

    for path in Path(result["run_dir"]).rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "555-66-7788" not in text
            assert "TopSecretDiagnosisXYZ" not in text
