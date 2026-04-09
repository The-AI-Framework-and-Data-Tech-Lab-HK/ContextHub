"""Unit tests for the ContextHub OpenClaw Plugin.

All tests mock the SDK — no real server dependency.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "openclaw" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk" / "src"))

from contexthub_sdk import ContextHubError, NotFoundError, SearchResponse, SearchResult
from contexthub_sdk.models import ContextStatus, ContextType, Scope

from openclaw.plugin import ContextHubContextEngine
from openclaw.tools import TOOL_DEFINITIONS, dispatch


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.memory = AsyncMock()
    client.skill = AsyncMock()
    client.search = AsyncMock()
    client.ls = AsyncMock()
    client.read = AsyncMock()
    client.grep = AsyncMock()
    client.stat = AsyncMock()
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def engine(mock_client):
    return ContextHubContextEngine(mock_client)


# ── §8.1: Tool definition completeness ─────────────────────────────────

EXPECTED_TOOLS = [
    "ls", "read", "grep", "stat",
    "contexthub_store", "contexthub_promote", "contexthub_skill_publish",
]


class TestToolDefinitions:
    """§8.1: Each tool has name, description, parameters JSON Schema."""

    def test_all_seven_tools_present(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert sorted(names) == sorted(EXPECTED_TOOLS)

    @pytest.mark.parametrize("tool", TOOL_DEFINITIONS, ids=lambda t: t["name"])
    def test_tool_has_required_fields(self, tool):
        assert "name" in tool
        assert "description" in tool
        assert isinstance(tool["description"], str) and len(tool["description"]) > 0
        assert "parameters" in tool
        params = tool["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params
        assert "required" in params


# ── §8.2: Each tool calls the correct SDK method ───────────────────────


class TestToolDispatch:

    @pytest.mark.asyncio
    async def test_ls_calls_client_ls(self, mock_client):
        mock_client.ls.return_value = ["a", "b"]
        result = await dispatch(mock_client, "ls", {"path": "datalake/"})
        mock_client.ls.assert_awaited_once_with("datalake/")
        assert json.loads(result) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_read_calls_client_read(self, mock_client):
        mock_client.read.return_value = MagicMock(
            model_dump=MagicMock(return_value={"uri": "x", "level": "L1", "content": "hello"})
        )
        result = await dispatch(mock_client, "read", {"uri": "x"})
        mock_client.read.assert_awaited_once()
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_grep_calls_client_grep(self, mock_client):
        mock_client.grep.return_value = MagicMock(
            model_dump=MagicMock(return_value={"results": [], "total": 0})
        )
        result = await dispatch(mock_client, "grep", {"query": "test"})
        mock_client.grep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stat_calls_client_stat(self, mock_client):
        mock_client.stat.return_value = MagicMock(
            model_dump=MagicMock(return_value={"uri": "x", "version": 1})
        )
        result = await dispatch(mock_client, "stat", {"uri": "x"})
        mock_client.stat.assert_awaited_once_with("x")

    @pytest.mark.asyncio
    async def test_store_calls_memory_add(self, mock_client):
        mock_client.memory.add.return_value = MagicMock(
            model_dump=MagicMock(return_value={"uri": "mem://1"})
        )
        result = await dispatch(mock_client, "contexthub_store", {"content": "hello"})
        mock_client.memory.add.assert_awaited_once_with(content="hello")

    @pytest.mark.asyncio
    async def test_promote_calls_memory_promote(self, mock_client):
        mock_client.memory.promote.return_value = MagicMock(
            model_dump=MagicMock(return_value={"uri": "team://1"})
        )
        result = await dispatch(
            mock_client, "contexthub_promote",
            {"uri": "mem://1", "target_team": "analytics"},
        )
        mock_client.memory.promote.assert_awaited_once_with(
            uri="mem://1", target_team="analytics"
        )

    @pytest.mark.asyncio
    async def test_skill_publish_calls_skill_publish(self, mock_client):
        mock_client.skill.publish.return_value = MagicMock(
            model_dump=MagicMock(return_value={"version": 1})
        )
        result = await dispatch(
            mock_client, "contexthub_skill_publish",
            {"skill_uri": "skill://x", "content": "SELECT 1"},
        )
        mock_client.skill.publish.assert_awaited_once()


# ── §8.3: SDK exceptions → agent-readable error ────────────────────────


class TestToolErrorHandling:

    @pytest.mark.asyncio
    async def test_sdk_error_returns_error_json(self, mock_client):
        mock_client.ls.side_effect = NotFoundError("not found")
        result = await dispatch(mock_client, "ls", {"path": "x"})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "not found" in parsed["error"]

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, mock_client):
        result = await dispatch(mock_client, "nonexistent", {})
        parsed = json.loads(result)
        assert "error" in parsed


# ── §8.4-6: assemble ───────────────────────────────────────────────────


class TestAssemble:

    @pytest.mark.asyncio
    async def test_does_not_modify_messages(self, engine, mock_client):
        msgs = [{"role": "user", "content": "hello"}]
        original = [m.copy() for m in msgs]
        mock_client.search.return_value = SearchResponse(results=[], total=0)
        result = await engine.assemble(sessionId="s1", messages=msgs)
        assert result["messages"] is msgs
        assert msgs == original

    @pytest.mark.asyncio
    async def test_returns_system_prompt_addition(self, engine, mock_client):
        mock_client.search.return_value = SearchResponse(
            results=[
                SearchResult(
                    uri="ctx://a", context_type=ContextType.MEMORY,
                    scope=Scope.AGENT, score=0.9,
                    l0_content="short", l1_content="detailed recall",
                    status=ContextStatus.ACTIVE, version=1,
                )
            ],
            total=1,
        )
        result = await engine.assemble(
            sessionId="s1", messages=[{"role": "user", "content": "query"}]
        )
        assert "systemPromptAddition" in result
        assert "detailed recall" in result["systemPromptAddition"]
        assert result["estimatedTokens"] > 0
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_recall_failure_degrades_gracefully(self, engine, mock_client):
        mock_client.search.side_effect = ContextHubError("boom")
        result = await engine.assemble(
            sessionId="s1", messages=[{"role": "user", "content": "query"}]
        )
        assert "ContextHub Tools Guide" in result["systemPromptAddition"]
        assert "Auto-Recall" not in result["systemPromptAddition"]
        assert result["messages"] == [{"role": "user", "content": "query"}]
        assert result["estimatedTokens"] > 0

    @pytest.mark.asyncio
    async def test_no_user_message_returns_guide_only(self, engine, mock_client):
        result = await engine.assemble(
            sessionId="s1", messages=[{"role": "system", "content": "sys"}]
        )
        assert "ContextHub Tools Guide" in result["systemPromptAddition"]
        assert "Auto-Recall" not in result["systemPromptAddition"]
        assert result["estimatedTokens"] > 0
        mock_client.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_token_budget_can_skip_recall(self, engine, mock_client):
        mock_client.search.return_value = SearchResponse(
            results=[
                SearchResult(
                    uri="ctx://a", context_type=ContextType.MEMORY,
                    scope=Scope.AGENT, score=0.9,
                    l0_content="short", l1_content="detailed recall",
                    status=ContextStatus.ACTIVE, version=1,
                )
            ],
            total=1,
        )
        result = await engine.assemble(
            sessionId="s1",
            messages=[{"role": "user", "content": "query"}],
            tokenBudget=6,
        )
        assert "ContextHub Tools Guide" in result["systemPromptAddition"]
        assert "Auto-Recall" not in result["systemPromptAddition"]
        assert result["estimatedTokens"] > 6
        mock_client.search.assert_not_awaited()


# ── §8.7-8: afterTurn ──────────────────────────────────────────────────


class TestAfterTurn:

    @pytest.mark.asyncio
    async def test_captures_reusable_assistant_facts(self, engine, mock_client):
        msgs = [
            {"role": "user", "content": "explain X"},
            {
                "role": "assistant",
                "content": (
                    "Here is the fix. "
                    "Use header `X-API-Key` on every request. "
                    "PATCH and DELETE require `If-Match`. "
                    "Let me know if you want more detail."
                ),
            },
        ]
        await engine.afterTurn(sessionId="s1", messages=msgs, prePromptMessageCount=0)
        mock_client.memory.add.assert_awaited_once()
        call_kwargs = mock_client.memory.add.call_args.kwargs
        assert "Use header `X-API-Key` on every request." in call_kwargs["content"]
        assert "PATCH and DELETE require `If-Match`." in call_kwargs["content"]
        assert "Let me know" not in call_kwargs["content"]
        assert "auto-capture" in call_kwargs["tags"]

    @pytest.mark.asyncio
    async def test_skips_short_content(self, engine, mock_client):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ]
        await engine.afterTurn(sessionId="s1", messages=msgs, prePromptMessageCount=0)
        mock_client.memory.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_generic_long_content(self, engine, mock_client):
        msgs = [
            {"role": "user", "content": "explain X"},
            {
                "role": "assistant",
                "content": (
                    "This explanation walks through the background and tradeoffs in a "
                    "general way without introducing any durable constraints or reusable "
                    "commands for future turns. "
                ) * 2,
            },
        ]
        await engine.afterTurn(sessionId="s1", messages=msgs, prePromptMessageCount=0)
        mock_client.memory.add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_failure_does_not_raise(self, engine, mock_client):
        mock_client.memory.add.side_effect = ContextHubError("fail")
        msgs = [
            {"role": "user", "content": "explain X"},
            {
                "role": "assistant",
                "content": (
                    "Use header `X-API-Key` on every request. "
                    "PATCH and DELETE require `If-Match`."
                ),
            },
        ]
        # Should not raise
        await engine.afterTurn(sessionId="s1", messages=msgs, prePromptMessageCount=0)


# ── §8.9: ingest / ingestBatch ──────────────────────────────────────────


class TestIngest:

    @pytest.mark.asyncio
    async def test_ingest_noop(self, engine):
        result = await engine.ingest(sessionId="s1", message={"role": "user", "content": "x"})
        assert result == {"ingested": False}

    @pytest.mark.asyncio
    async def test_ingest_batch_noop(self, engine):
        result = await engine.ingestBatch(sessionId="s1", messages=[])
        assert result == {"ingested": False}


# ── §8.10: compact ──────────────────────────────────────────────────────


class TestCompact:

    @pytest.mark.asyncio
    async def test_compact_returns_not_compacted(self, engine):
        result = await engine.compact(sessionId="s1")
        assert result == {"compacted": False}


# ── §8.11: naming contract ─────────────────────────────────────────────


class TestNamingContract:

    def test_info_has_required_fields(self, engine):
        info = engine.info
        assert info["kind"] == "context-engine"
        assert info["id"] == "contexthub"

    def test_public_methods_are_camel_case(self, engine):
        assert hasattr(engine, "assemble")
        assert hasattr(engine, "afterTurn")
        assert hasattr(engine, "ingest")
        assert hasattr(engine, "ingestBatch")
        assert hasattr(engine, "compact")
        assert hasattr(engine, "dispose")

    @pytest.mark.asyncio
    async def test_assemble_return_keys(self, engine, mock_client):
        mock_client.search.return_value = SearchResponse(results=[], total=0)
        result = await engine.assemble(
            sessionId="s1", messages=[{"role": "user", "content": "q"}]
        )
        assert set(result.keys()) == {"messages", "estimatedTokens", "systemPromptAddition"}

    @pytest.mark.asyncio
    async def test_ingest_return_key(self, engine):
        result = await engine.ingest(sessionId="s1", message={})
        assert set(result.keys()) == {"ingested"}

    @pytest.mark.asyncio
    async def test_compact_return_key(self, engine):
        result = await engine.compact(sessionId="s1")
        assert set(result.keys()) == {"compacted"}
