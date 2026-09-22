from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from contexthub.enforcement.context import Boundary
from contexthub.enforcement.decision import GuardrailDecision, Verdict
from integrations.entcollabbench.mcp_runtime_adapter import McpEndpointConfig, ToolSchemaCache
from integrations.entcollabbench.runtime_wrapper import ContextHubRuntimeWrapper


pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")


def _live_schema_cache(tools: list[dict], calls: list[str] | None = None) -> ToolSchemaCache:
    import json

    config = McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"})

    def opener(request, timeout):
        method = json.loads(request.data.decode("utf-8"))["method"]
        if calls is not None:
            calls.append(method)
        if method == "initialize":
            return _FakeResponse({"result": {"protocolVersion": "2024-11-05"}})
        return _FakeResponse({"result": {"tools": tools}})

    return ToolSchemaCache(config, opener=opener)


class FakeRepo:
    @asynccontextmanager
    async def session(self, account_id: str):
        yield {"account_id": account_id}


class FakeService:
    def __init__(self, decision: GuardrailDecision):
        self.decision = decision
        self.calls = []

    async def enforce(self, db, ec):
        self.calls.append((db, ec))
        return self.decision


def _message_schema(enum: list[str] | None = None) -> dict:
    content_spec = {"type": "string"}
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


async def test_valid_tool_args_allow_before_execute() -> None:
    wrapper = ContextHubRuntimeWrapper()

    result = await wrapper.enforce_tool_call_before_execute(
        agent_id="collaboration_ops_specialist",
        server="teams",
        tool_name="send_channel_message",
        raw_args={
            "team_id": "team_techcorp_001",
            "channel_id": "channel_shared_001",
            "content": "hello",
        },
        schema_record=_message_schema(),
    )

    assert result.decision.verdict == Verdict.ALLOW
    assert result.action.action == "allow"
    assert result.action.allow is True
    assert result.normalized_args["teamId"] == "team_techcorp_001"
    assert result.normalized_args["channelId"] == "channel_shared_001"
    assert result.contract["required_role"] == "collaboration_ops_specialist"


async def test_invalid_enum_repairs_before_execute() -> None:
    wrapper = ContextHubRuntimeWrapper()

    result = await wrapper.enforce_tool_call_before_execute(
        agent_id="collaboration_ops_specialist",
        server="teams",
        tool_name="send_channel_message",
        raw_args={
            "teamId": "team_techcorp_001",
            "channelId": "channel_shared_001",
            "content": "not-approved",
        },
        schema_record=_message_schema(enum=["approved"]),
    )

    assert result.decision.verdict == Verdict.REPAIR
    assert result.action.action == "retry_with_patch"
    assert result.action.patch == {"content": "approved"}


async def test_live_schema_cache_repairs_enum_and_amortizes_tools_list() -> None:
    calls: list[str] = []
    cache = _live_schema_cache(
        [
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
            },
            {"name": "list_teams", "inputSchema": {"type": "object", "properties": {}, "required": []}},
        ],
        calls,
    )
    wrapper = ContextHubRuntimeWrapper(schema_cache=cache)

    repaired = await wrapper.enforce_tool_call_before_execute(
        agent_id="collaboration_ops_specialist",
        server="teams",
        tool_name="send_channel_message",
        raw_args={"teamId": "t", "channelId": "c", "content": "not-approved"},
    )
    assert repaired.schema_source == "live-mcp-schema"
    assert repaired.action.action == "retry_with_patch"
    assert repaired.action.patch == {"content": "approved"}

    # A second tool on the same server is served from cache (one tools/list total).
    await wrapper.enforce_tool_call_before_execute(
        agent_id="collaboration_ops_specialist",
        server="teams",
        tool_name="list_teams",
        raw_args={},
    )
    assert calls.count("tools/list") == 1


async def test_endpoint_config_auto_builds_schema_cache() -> None:
    wrapper = ContextHubRuntimeWrapper(
        endpoint_config=McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"})
    )
    assert wrapper._schema_cache is not None


async def test_wrong_role_blocks_before_execute() -> None:
    wrapper = ContextHubRuntimeWrapper()

    result = await wrapper.enforce_tool_call_before_execute(
        agent_id="it_service_desk_l1",
        server="teams",
        tool_name="send_channel_message",
        raw_args={
            "teamId": "team_techcorp_001",
            "channelId": "channel_shared_001",
            "content": "hello",
        },
        schema_record=_message_schema(),
        required_role="collaboration_ops_specialist",
    )

    assert result.decision.verdict == Verdict.BLOCK
    assert result.action.action == "block"
    assert result.action.allow is False


async def test_completed_closure_allows_after_result() -> None:
    wrapper = ContextHubRuntimeWrapper()

    result = await wrapper.enforce_closure_after_result_or_timeout(
        agent_id="collaboration_ops_specialist",
        workflow_id="mcp_single_smoke",
        ground_truth=[
            {
                "agent": "collaboration_ops_specialist",
                "mcp_server_name": "teams",
                "tool_name": "send_channel_message",
            }
        ],
        trace_events=[
            _tool_call_event("call-1"),
            _tool_result_event("call-1", status="success"),
        ],
        runtime_summary={"status": "passed", "timeout": False},
    )

    assert result.decision.verdict == Verdict.ALLOW
    assert result.action.action == "allow"
    assert result.missing_actions == []
    assert result.open_questions == []


async def test_missing_required_action_and_timeout_blocks_closure() -> None:
    wrapper = ContextHubRuntimeWrapper()

    result = await wrapper.enforce_closure_after_result_or_timeout(
        agent_id="collaboration_ops_specialist",
        workflow_id="mcp_single_timeout",
        ground_truth=[
            {
                "agent": "knowledge_base_specialist",
                "mcp_server_name": "knowledge",
                "tool_name": "update_knowledge",
            }
        ],
        trace_events=[],
        runtime_summary={"status": "timeout", "timeout": True, "failure_reason": "timed out"},
    )

    assert result.decision.verdict == Verdict.BLOCK
    assert result.action.action == "block"
    assert result.missing_actions == ["knowledge_base_specialist.update_knowledge"]
    assert result.open_questions == [
        "timeout_or_partial_trace: run ended before a clean terminal closure boundary",
        "missing_required_action: knowledge_base_specialist.update_knowledge",
    ]


async def test_fake_service_injection_captures_tool_context_payload() -> None:
    service = FakeService(GuardrailDecision(Verdict.ALLOW, reason="captured"))
    wrapper = ContextHubRuntimeWrapper(
        repo=FakeRepo(),
        account_id="acme",
        service=service,
        schema_provider=lambda server, tool: _message_schema(),
    )

    result = await wrapper.enforce_tool_call_before_execute(
        agent_id="collaboration_ops_specialist",
        server="teams",
        tool_name="send_channel_message",
        raw_args={"team_id": "team-1", "channel_id": "channel-1", "content": "hello"},
    )

    assert result.decision.reason == "captured"
    db, ec = service.calls[0]
    assert db == {"account_id": "acme"}
    assert ec.boundary == Boundary.TOOL_CALL
    assert ec.actor.agent_id == "collaboration_ops_specialist"
    assert ec.payload["contract"]["tool_name"] == "send_channel_message"
    assert ec.payload["tool_args"]["teamId"] == "team-1"
    assert ec.declared_context_uris == ["ctx://entcollab/tool_schema/teams"]


async def test_fake_service_injection_captures_closure_context_payload() -> None:
    service = FakeService(GuardrailDecision(Verdict.ALLOW, reason="closure captured"))
    wrapper = ContextHubRuntimeWrapper(repo=FakeRepo(), account_id="acme", service=service)

    await wrapper.enforce_closure_after_result_or_timeout(
        agent_id="collaboration_ops_specialist",
        workflow_id="wf-capture",
        ground_truth=[],
        trace_events=[],
        runtime_summary={"status": "passed"},
        declared_context_uris=["ctx://entcollab/policy/P1@v1"],
    )

    _, ec = service.calls[0]
    assert ec.boundary == Boundary.CLOSURE
    assert ec.actor.agent_id == "collaboration_ops_specialist"
    assert ec.workflow_id == "wf-capture"
    assert ec.payload["anchor"]["workflow_id"] == "wf-capture"
    assert ec.declared_context_uris == ["ctx://entcollab/policy/P1@v1"]


def _tool_call_event(call_id: str) -> dict:
    return {
        "event": "tool_call",
        "agent_name": "collaboration_ops_specialist",
        "ts": 1.0,
        "data": {
            "tool_call_id": call_id,
            "tool_name": "mcp_teams_call_tool",
            "arguments": {
                "tool_name": "send_channel_message",
                "arguments_json": '{"teamId": "team_techcorp_001", "channelId": "channel_shared_001", "content": "hello"}',
            },
        },
    }


def _tool_result_event(call_id: str, *, status: str) -> dict:
    return {
        "event": "tool_result",
        "agent_name": "collaboration_ops_specialist",
        "ts": 2.0,
        "data": {
            "tool_call_id": call_id,
            "tool_name": "send_channel_message",
            "status": status,
        },
    }
