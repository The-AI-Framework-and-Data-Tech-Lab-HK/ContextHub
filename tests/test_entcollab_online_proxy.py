from __future__ import annotations

from contextlib import asynccontextmanager
import json

import pytest

from contexthub.enforcement.decision import GuardrailDecision, Verdict, Violation, ViolationKind
from contexthub.enforcement.staleness import StalenessResult
from integrations.entcollabbench.interceptor import EnforcementAction
from integrations.entcollabbench.online_proxy import (
    ENFORCE,
    OBSERVE,
    DecisionRecord,
    ForwardResponse,
    McpToolProxy,
    ToolProxyRoute,
    build_s1_tool_proxy,
)
from integrations.entcollabbench.runtime_wrapper import (
    ContextHubRuntimeWrapper,
    RuntimeToolEnforcementResult,
)
from integrations.entcollabbench.world_loader import LoadedWorld


pytestmark = pytest.mark.asyncio


ROUTE = ToolProxyRoute(
    agent_id="collaboration_ops_specialist",
    server="teams",
    upstream_endpoint="http://127.0.0.1:9001/mcp",
)


def _message_schema(enum: list[str] | None = None) -> dict:
    content_spec: dict = {"type": "string"}
    if enum is not None:
        content_spec["enum"] = enum
    return {
        "name": "send_channel_message",
        "inputSchema": {
            "type": "object",
            "properties": {
                "teamId": {"type": "string"},
                "channelId": {"type": "string"},
                "content": content_spec,
            },
            "required": ["teamId", "channelId", "content"],
        },
    }


def _tools_call_body(tool: str, arguments: dict, rid: int = 1) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ).encode("utf-8")


class FakeUpstream:
    def __init__(self, response: ForwardResponse | None = None):
        self.calls: list[dict] = []
        self._response = response or ForwardResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=b'{"ok":true}',
        )

    def __call__(self, route: ToolProxyRoute, headers, body: bytes) -> ForwardResponse:
        self.calls.append({"route": route, "headers": dict(headers), "body": body})
        return self._response


class StubWrapper:
    """Minimal stand-in exposing only the method the proxy calls."""

    def __init__(self, result: RuntimeToolEnforcementResult | None = None, *, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    async def enforce_tool_call_before_execute(self, **kwargs) -> RuntimeToolEnforcementResult:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _block_result() -> RuntimeToolEnforcementResult:
    decision = GuardrailDecision(
        Verdict.BLOCK,
        violations=[
            Violation(
                ViolationKind.UNAUTHORIZED_FLOW,
                "agent lacks role for tool",
                evidence={"tool": "send_channel_message"},
            )
        ],
        reason="tool_state guardrail: ['unauthorized_flow']",
        guardrail="tool_state",
    )
    action = EnforcementAction(action="block", allow=False, decision=decision)
    return RuntimeToolEnforcementResult(
        decision=decision,
        action=action,
        contract={},
        normalized_args={"teamId": "t", "channelId": "c", "content": "hi"},
        schema_source="stub",
    )


def _repair_result(*, normalized_args: dict, patch: dict) -> RuntimeToolEnforcementResult:
    decision = GuardrailDecision(
        Verdict.REPAIR,
        violations=[
            Violation(
                ViolationKind.SCHEMA_OR_ENUM,
                "arg 'content' not in enum ['approved']",
                repair_hint={"arg": "content", "allowed": ["approved"], "got": "bad"},
            )
        ],
        reason="tool_state guardrail: ['schema_or_enum']",
        guardrail="tool_state",
    )
    action = EnforcementAction(
        action="retry_with_patch",
        retry=True,
        patch=patch,
        decision=decision,
    )
    return RuntimeToolEnforcementResult(
        decision=decision,
        action=action,
        contract={},
        normalized_args=normalized_args,
        schema_source="stub",
    )


def _valid_args() -> dict:
    return {"teamId": "team_techcorp_001", "channelId": "channel_shared_001", "content": "hello"}


async def test_observe_always_forwards_original_on_allow() -> None:
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(schema_provider=lambda s, t: _message_schema())
    proxy = McpToolProxy(wrapper, mode=OBSERVE, upstream=upstream)
    body = _tools_call_body("send_channel_message", _valid_args())

    outcome = await proxy.handle(ROUTE, headers={"Content-Type": "application/json"}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.verdict == "allow"
    assert outcome.record.forwarded is True
    assert outcome.record.would_block is False


async def test_observe_forwards_even_when_verdict_would_block() -> None:
    upstream = FakeUpstream()
    proxy = McpToolProxy(StubWrapper(_block_result()), mode=OBSERVE, upstream=upstream)
    body = _tools_call_body("send_channel_message", _valid_args())

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    # Observe never changes the outcome: upstream is still contacted with the original body.
    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.verdict == "block"
    assert outcome.record.would_block is True
    assert outcome.record.forwarded is True


async def test_enforce_block_does_not_contact_upstream() -> None:
    upstream = FakeUpstream()
    proxy = McpToolProxy(StubWrapper(_block_result()), mode=ENFORCE, upstream=upstream)
    body = _tools_call_body("send_channel_message", _valid_args())

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []  # the real MCP service is never reached
    payload = json.loads(outcome.response.body)
    assert payload["result"]["isError"] is True
    assert payload["result"]["_contexthub"]["blocked"] is True
    assert payload["id"] == 1
    assert outcome.record is not None
    assert outcome.record.forwarded is False


async def test_enforce_allow_forwards_original() -> None:
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(schema_provider=lambda s, t: _message_schema())
    proxy = McpToolProxy(wrapper, mode=ENFORCE, upstream=upstream)
    body = _tools_call_body("send_channel_message", _valid_args())

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None and outcome.record.verdict == "allow"
    assert outcome.record.patched is False


async def test_enforce_patches_enum_arg_before_forwarding() -> None:
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(schema_provider=lambda s, t: _message_schema(enum=["approved"]))
    proxy = McpToolProxy(wrapper, mode=ENFORCE, upstream=upstream)
    body = _tools_call_body(
        "send_channel_message",
        {"teamId": "t", "channelId": "c", "content": "not-approved"},
    )

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert outcome.record is not None
    assert outcome.record.action == "retry_with_patch"
    assert outcome.record.patched is True
    assert upstream.calls, "patched request must still reach upstream"
    forwarded = json.loads(upstream.calls[0]["body"])
    assert forwarded["params"]["arguments"]["content"] == "approved"


async def test_enforce_patch_uses_normalized_args_as_base() -> None:
    upstream = FakeUpstream()
    result = _repair_result(
        normalized_args={
            "teamId": "t",
            "channelId": "c",
            "content": "bad",
            "body": {"content": "bad"},
        },
        patch={"content": "approved"},
    )
    proxy = McpToolProxy(StubWrapper(result), mode=ENFORCE, upstream=upstream)
    body = _tools_call_body(
        "send_channel_message",
        {"teamId": "t", "channelId": "c", "body": {"content": "bad"}},
    )

    await proxy.handle(ROUTE, headers={"Content-Length": "123"}, body=body)

    forwarded = json.loads(upstream.calls[0]["body"])
    forwarded_args = forwarded["params"]["arguments"]
    assert forwarded_args["content"] == "approved"
    assert forwarded_args["body"] == {"content": "bad"}
    assert "Content-Length" not in upstream.calls[0]["headers"]


async def test_enforce_missing_required_arg_synthesizes_error() -> None:
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(schema_provider=lambda s, t: _message_schema())
    proxy = McpToolProxy(wrapper, mode=ENFORCE, upstream=upstream)
    body = _tools_call_body("send_channel_message", {"content": "hello"})

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []
    payload = json.loads(outcome.response.body)
    assert payload["result"]["isError"] is True
    assert outcome.record is not None
    assert outcome.record.would_block is True
    assert outcome.record.forwarded is False


async def test_s1_tool_proxy_uses_generic_schema_guardrail_only() -> None:
    upstream = FakeUpstream()
    proxy = build_s1_tool_proxy(
        mode=ENFORCE,
        schema_provider=lambda s, t: _message_schema(),
        upstream=upstream,
    )
    body = _tools_call_body("send_channel_message", {"content": "hello"})

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []
    assert outcome.record is not None
    assert outcome.record.verdict == "repair"
    assert outcome.record.action in {"retry_with_feedback", "retry_with_patch"}
    assert outcome.record.forwarded is False
    assert outcome.record.schema_source == "injected-schema-provider"


async def test_non_tools_call_passthrough_skips_gate() -> None:
    upstream = FakeUpstream()
    provider_calls: list[tuple[str, str]] = []

    def provider(server: str, tool: str) -> dict:
        provider_calls.append((server, tool))
        return _message_schema()

    wrapper = ContextHubRuntimeWrapper(schema_provider=provider)
    proxy = McpToolProxy(wrapper, mode=ENFORCE, upstream=upstream)
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}).encode()

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert outcome.record is None
    assert provider_calls == []  # the gate is never invoked for non tools/call
    assert upstream.calls and upstream.calls[0]["body"] == body


async def test_decision_sink_receives_record() -> None:
    sink_records: list[DecisionRecord] = []
    wrapper = ContextHubRuntimeWrapper(schema_provider=lambda s, t: _message_schema())
    proxy = McpToolProxy(
        wrapper,
        mode=OBSERVE,
        upstream=FakeUpstream(),
        decision_sink=sink_records.append,
    )
    body = _tools_call_body("send_channel_message", _valid_args())

    await proxy.handle(ROUTE, headers={}, body=body)

    assert len(sink_records) == 1
    assert sink_records[0].tool_name == "send_channel_message"
    assert proxy.records == sink_records


async def test_gate_error_fails_open_and_forwards() -> None:
    upstream = FakeUpstream()
    proxy = McpToolProxy(
        StubWrapper(raises=RuntimeError("schema lookup exploded")),
        mode=ENFORCE,
        upstream=upstream,
    )
    body = _tools_call_body("send_channel_message", _valid_args())

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.verdict == "error"
    assert "schema lookup exploded" in (outcome.record.error or "")


async def test_audit_sink_failure_does_not_affect_enforcement() -> None:
    class RaisingAuditSink:
        def __init__(self):
            self.calls = 0

        def __call__(self, record):
            self.calls += 1
            raise RuntimeError("audit down")

    upstream = FakeUpstream()
    audit = RaisingAuditSink()
    proxy = McpToolProxy(
        StubWrapper(_block_result()),
        mode=ENFORCE,
        upstream=upstream,
        audit_sink=audit,
    )

    outcome = await proxy.handle(ROUTE, headers={}, body=_tools_call_body("send_channel_message", _valid_args()))

    assert audit.calls == 1
    assert upstream.calls == []
    assert outcome.record is not None and outcome.record.verdict == "block"


async def test_mode_validation_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        McpToolProxy(StubWrapper(_block_result()), mode="dry-run")


async def test_enforce_batch_with_tools_call_is_blocked_not_bypassed() -> None:
    upstream = FakeUpstream()
    proxy = McpToolProxy(StubWrapper(_block_result()), mode=ENFORCE, upstream=upstream)
    body = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "send_channel_message", "arguments": _valid_args()},
            },
        ]
    ).encode("utf-8")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls == []  # the smuggled tools/call never reaches upstream
    payload = json.loads(outcome.response.body)
    assert payload["error"]["code"] == -32600
    assert outcome.record is not None
    assert outcome.record.verdict == "batch_unsupported"
    assert outcome.record.would_block is True


async def test_observe_batch_forwards_and_records() -> None:
    upstream = FakeUpstream()
    proxy = McpToolProxy(StubWrapper(_block_result()), mode=OBSERVE, upstream=upstream)
    body = json.dumps(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "send_channel_message", "arguments": _valid_args()},
            }
        ]
    ).encode("utf-8")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body  # observe never alters outcome
    assert outcome.record is not None
    assert outcome.record.verdict == "batch_unsupported"
    assert outcome.record.would_block is False
    assert outcome.record.forwarded is True


async def test_batch_without_tools_call_passes_through() -> None:
    upstream = FakeUpstream()
    proxy = McpToolProxy(StubWrapper(_block_result()), mode=ENFORCE, upstream=upstream)
    body = json.dumps(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}]
    ).encode("utf-8")

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is None  # nothing gated, transparent passthrough


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


async def test_observe_records_live_schema_would_repair() -> None:
    from integrations.entcollabbench.mcp_runtime_adapter import McpEndpointConfig, ToolSchemaCache

    tools = [
        {
            "name": "send_channel_message",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "teamId": {"type": "string"},
                    "channelId": {"type": "string"},
                    "content": {"type": "string", "enum": ["approved"]},
                },
                "required": ["teamId", "channelId", "content"],
            },
        }
    ]

    def opener(request, timeout):
        method = json.loads(request.data.decode("utf-8"))["method"]
        if method == "initialize":
            return _FakeResp({"result": {"protocolVersion": "2024-11-05"}})
        return _FakeResp({"result": {"tools": tools}})

    cache = ToolSchemaCache(
        McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"}), opener=opener
    )
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(schema_cache=cache)
    proxy = McpToolProxy(wrapper, mode=OBSERVE, upstream=upstream)
    body = _tools_call_body("send_channel_message", {"teamId": "t", "channelId": "c", "content": "bad"})

    outcome = await proxy.handle(ROUTE, headers={}, body=body)

    # Observe still forwards the original, but now records a live-schema would_repair.
    assert upstream.calls and upstream.calls[0]["body"] == body
    assert outcome.record is not None
    assert outcome.record.schema_source == "live-mcp-schema"
    assert outcome.record.would_repair is True
    assert outcome.record.forwarded is True


async def test_schema_provider_is_reused_for_gate() -> None:
    provider_calls: list[tuple[str, str]] = []

    def provider(server: str, tool: str) -> dict:
        provider_calls.append((server, tool))
        return _message_schema()

    wrapper = ContextHubRuntimeWrapper(schema_provider=provider)
    proxy = McpToolProxy(wrapper, mode=OBSERVE, upstream=FakeUpstream())
    body = _tools_call_body("send_channel_message", _valid_args())

    await proxy.handle(ROUTE, headers={}, body=body)

    assert provider_calls == [("teams", "send_channel_message")]


class FakeRepo:
    @asynccontextmanager
    async def session(self, account_id: str):
        yield object()


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


async def same_role(agent_id: str, required_role: str) -> bool:
    return agent_id == required_role


async def allow_role(agent_id: str, required_role: str) -> bool:
    return True


async def missing_object(object_id: str) -> bool:
    return False


async def test_full_s2_wrapper_blocks_stale_tool_schema_before_upstream() -> None:
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(
        repo=FakeRepo(),
        loaded=LoadedWorld(),
        schema_provider=lambda s, t: _message_schema(),
        staleness=StaleEverything(),
        role_checker=same_role,
    )
    proxy = McpToolProxy(wrapper, mode=ENFORCE, upstream=upstream)

    outcome = await proxy.handle(
        ROUTE,
        headers={},
        body=_tools_call_body("send_channel_message", _valid_args()),
    )

    assert upstream.calls == []
    assert outcome.record is not None
    assert outcome.record.verdict == "repair"
    assert outcome.record.action == "pending"
    assert outcome.record.would_block is True


async def test_full_s2_wrapper_blocks_missing_update_target_object() -> None:
    upstream = FakeUpstream()
    wrapper = ContextHubRuntimeWrapper(
        repo=FakeRepo(),
        loaded=LoadedWorld(),
        schema_provider=lambda s, t: {
            "name": "update_ticket",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
                "required": ["id", "status"],
            },
        },
        staleness=NoStaleRefs(),
        role_checker=allow_role,
        object_exists=missing_object,
    )
    proxy = McpToolProxy(wrapper, mode=ENFORCE, upstream=upstream)

    outcome = await proxy.handle(
        ToolProxyRoute("it_change_engineer", "itsm", "http://127.0.0.1:8006/mcp"),
        headers={},
        body=_tools_call_body("update_ticket", {"id": "INC-404", "status": "closed"}),
    )

    assert upstream.calls == []
    assert outcome.record is not None
    assert outcome.record.verdict == "block"
    assert any(v["kind"] == "wrong_object_mutation" for v in outcome.record.violations)


class NoStaleRefs:
    async def any_stale_or_blocked_refs(self, db, refs):
        return []
