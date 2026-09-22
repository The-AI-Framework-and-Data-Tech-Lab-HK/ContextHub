from __future__ import annotations

import json

import pytest

from contexthub.enforcement.decision import GuardrailDecision, Verdict, Violation, ViolationKind
from contexthub.enforcement.staleness import StalenessResult
from integrations.entcollabbench.interceptor import EnforcementAction
from integrations.entcollabbench.online_handoff_proxy import (
    ENFORCE,
    OBSERVE,
    AgentHandoffProxy,
    ForwardResponse,
    HandoffDecisionRecord,
    HandoffProxyRoute,
    build_handoff_packet,
    build_s1_handoff_proxy,
    build_weak_s2_handoff_proxy,
)
from integrations.entcollabbench.runtime_wrapper import (
    ContextHubRuntimeWrapper,
    RuntimeHandoffEnforcementResult,
)
from integrations.entcollabbench.world_loader import LoadedWorld


pytestmark = pytest.mark.asyncio


ROUTE = HandoffProxyRoute(
    target_agent="it_service_desk_l1",
    upstream_endpoint="http://127.0.0.1:9100/v1/agent/tasks",
)


def _delegation_body(task: str, *, source: str = "hr_service_specialist", rid: str = "req-1") -> bytes:
    return json.dumps(
        {
            "request_id": rid,
            "source_agent": source,
            "target_agent": "it_service_desk_l1",
            "task": task,
            "recursion": {"depth": 1, "max_depth": 5, "trace": []},
            "metadata": {"session_id": "sess-1"},
        }
    ).encode("utf-8")


class FakeUpstream:
    def __init__(self, response: ForwardResponse | None = None):
        self.calls: list[dict] = []
        self._response = response or ForwardResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=b'{"status":"success"}',
        )

    def __call__(self, route, headers, body: bytes) -> ForwardResponse:
        self.calls.append({"route": route, "headers": dict(headers), "body": body})
        return self._response


class FakeRepo:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False

    def session(self, account_id: str):
        return self


class StaleEverything:
    async def any_stale_or_blocked_refs(self, db, refs):
        return [
            StalenessResult(
                uri=str(ref).split("@v", 1)[0],
                status="stale",
                is_stale=True,
                version_mismatch=False,
                is_blocked=False,
                is_unknown=False,
            )
            for ref in refs
        ]


class StubWrapper:
    def __init__(self, result: RuntimeHandoffEnforcementResult | None = None, *, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    async def enforce_handoff_before_delegate(self, **kwargs) -> RuntimeHandoffEnforcementResult:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _incomplete_result() -> RuntimeHandoffEnforcementResult:
    decision = GuardrailDecision(
        Verdict.REPAIR,
        violations=[
            Violation(
                ViolationKind.INCOMPLETE_HANDOFF,
                "missing required handoff fields: ['required_object_ids']",
                repair_hint={"missing_fields": ["required_object_ids"]},
            )
        ],
        reason="handoff guardrail: ['incomplete_handoff']",
        guardrail="handoff",
    )
    action = EnforcementAction(action="retry_with_feedback", allow=False, retry=True, decision=decision)
    return RuntimeHandoffEnforcementResult(decision=decision, action=action, packet={})


async def test_observe_forwards_even_when_verdict_would_block() -> None:
    upstream = FakeUpstream()
    proxy = AgentHandoffProxy(StubWrapper(_incomplete_result()), mode=OBSERVE, upstream=upstream)
    body = _delegation_body("do the thing")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.would_block is True
    assert outcome.record.forwarded is True


async def test_enforce_block_does_not_contact_downstream() -> None:
    upstream = FakeUpstream()
    proxy = AgentHandoffProxy(StubWrapper(_incomplete_result()), mode=ENFORCE, upstream=upstream)
    body = _delegation_body("do the thing")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []  # the downstream agent never starts
    payload = json.loads(outcome.response.body)
    # Calibrated to api_schema.AgentTaskResponse: a valid, parseable rejection.
    assert payload["status"] == "rejected"
    assert payload["api_version"] == "2026-04-04"
    assert payload["handled_by"] == "it_service_desk_l1"
    assert payload["request_id"] == "req-1"
    assert payload["result"] == ""
    assert "ContextHub S2 handoff blocked" in payload["error"]
    assert payload["recursion"] == {"depth": 1, "max_depth": 5, "trace": []}
    assert payload["_contexthub"]["blocked"] is True
    assert outcome.record is not None and outcome.record.forwarded is False


async def test_entry_task_without_source_agent_passes_through() -> None:
    upstream = FakeUpstream()
    proxy = AgentHandoffProxy(StubWrapper(_incomplete_result()), mode=ENFORCE, upstream=upstream)
    body = json.dumps({"task": "entry task", "metadata": {"session_id": "s"}}).encode("utf-8")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert outcome.record is None  # not gated
    assert upstream.calls and upstream.calls[0]["body"] == body


async def test_gate_error_fails_open_and_forwards() -> None:
    upstream = FakeUpstream()
    proxy = AgentHandoffProxy(StubWrapper(raises=RuntimeError("acl exploded")), mode=ENFORCE, upstream=upstream)
    body = _delegation_body("do the thing")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.verdict == "error"
    assert "acl exploded" in (outcome.record.error or "")


async def test_decision_sink_receives_record() -> None:
    sink_records: list[HandoffDecisionRecord] = []
    proxy = AgentHandoffProxy(
        StubWrapper(_incomplete_result()),
        mode=OBSERVE,
        upstream=FakeUpstream(),
        decision_sink=sink_records.append,
    )

    await proxy.handle(ROUTE, headers={}, body=_delegation_body("do the thing"))

    assert len(sink_records) == 1
    assert sink_records[0].recipient == "it_service_desk_l1"
    assert proxy.records == sink_records


async def test_real_weak_s2_enforce_allows_complete_handoff() -> None:
    upstream = FakeUpstream()
    proxy = build_weak_s2_handoff_proxy(mode=ENFORCE, upstream=upstream)
    body = _delegation_body("please handle case 57 for the customer")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.verdict == "allow"
    assert outcome.record.required_object_ids == ["case/57"]


async def test_real_weak_s2_enforce_blocks_incomplete_handoff() -> None:
    upstream = FakeUpstream()
    proxy = build_weak_s2_handoff_proxy(mode=ENFORCE, upstream=upstream)
    body = _delegation_body("do the thing")  # no extractable object id

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []
    assert outcome.record is not None
    assert "required_object_ids" in outcome.record.missing_fields
    assert outcome.record.forwarded is False


async def test_s1_handoff_proxy_uses_generic_handoff_shape_only() -> None:
    upstream = FakeUpstream()
    proxy = build_s1_handoff_proxy(mode=ENFORCE, upstream=upstream)
    body = _delegation_body("", source="hr_service_specialist")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []
    assert outcome.record is not None
    assert outcome.record.verdict == "repair"
    assert outcome.record.forwarded is False
    assert outcome.record.reason.startswith("generic handoff")


async def test_full_s2_handoff_blocks_stale_context_before_downstream() -> None:
    upstream = FakeUpstream()
    loaded = LoadedWorld()
    loaded.loaded_uris.add("ctx://entcollab/role/it_service_desk_l1")
    wrapper = ContextHubRuntimeWrapper(
        repo=FakeRepo(),
        loaded=loaded,
        staleness=StaleEverything(),
    )
    proxy = AgentHandoffProxy(wrapper, mode=ENFORCE, upstream=upstream)

    outcome = await proxy.handle(
        ROUTE,
        headers={},
        body=_delegation_body("do the thing"),
    )

    assert upstream.calls == []
    assert outcome.record is not None
    assert outcome.record.verdict == "repair"
    assert outcome.record.would_block is True
    assert any(v["kind"] == "stale_dependency" for v in outcome.record.violations)


async def test_build_handoff_packet_maps_delegation_fields() -> None:
    payload = {
        "source_agent": "hr_service_specialist",
        "target_agent": "it_service_desk_l1",
        "task": "resolve case 57 urgently",
    }

    packet = build_handoff_packet(payload, recipient="it_service_desk_l1")

    assert packet["sender"] == "hr_service_specialist"
    assert packet["recipient"] == "it_service_desk_l1"
    assert packet["task_intent"] == "resolve case 57 urgently"
    assert packet["expected_action"] == "complete_delegated_task"
    assert packet["required_object_ids"] == ["case/57"]
    assert packet["context_versions"] == ["ctx://entcollab/role/it_service_desk_l1"]
