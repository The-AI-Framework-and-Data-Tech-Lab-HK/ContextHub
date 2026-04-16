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
    client.report_feedback = AsyncMock()
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
    "ls",
    "read",
    "grep",
    "stat",
    "contexthub_store",
    "contexthub_promote",
    "contexthub_skill_publish",
    "contexthub_feedback",
]


class TestToolDefinitions:
    """§8.1: Each tool has name, description, parameters JSON Schema."""

    def test_all_eight_tools_present_in_order(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert names == EXPECTED_TOOLS

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

    def test_feedback_tool_schema_matches_contract(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "contexthub_feedback")
        params = tool["parameters"]
        assert params["required"] == ["context_uri", "outcome"]
        assert set(params["properties"]) == {"context_uri", "outcome", "retrieval_id", "metadata"}
        assert params["properties"]["outcome"]["enum"] == [
            "adopted",
            "ignored",
            "corrected",
            "irrelevant",
        ]


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
    async def test_feedback_calls_report_feedback(self, mock_client):
        mock_client.report_feedback.return_value = {"id": 1, "outcome": "adopted"}
        result = await dispatch(
            mock_client,
            "contexthub_feedback",
            {
                "context_uri": "ctx://team/engineering/resources/orders",
                "outcome": "adopted",
                "retrieval_id": "rid-1",
                "metadata": {"source": "explicit-search"},
            },
        )
        mock_client.report_feedback.assert_awaited_once_with(
            context_uri="ctx://team/engineering/resources/orders",
            outcome="adopted",
            retrieval_id="rid-1",
            metadata={"source": "explicit-search"},
        )
        assert json.loads(result)["outcome"] == "adopted"

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
        mock_client.search.return_value = SearchResponse(
            results=[],
            total=0,
            retrieval_id="550e8400-e29b-41d4-a716-446655440000",
        )
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
            retrieval_id="550e8400-e29b-41d4-a716-446655440001",
        )
        result = await engine.assemble(
            sessionId="s1", messages=[{"role": "user", "content": "query"}]
        )
        assert "systemPromptAddition" in result
        spa = result["systemPromptAddition"]
        assert "detailed recall" in spa
        assert "Auto-Recall" in spa
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
            retrieval_id="550e8400-e29b-41d4-a716-446655440002",
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

    @pytest.mark.asyncio
    async def test_tool_guide_includes_feedback_guidance(self, engine, mock_client):
        mock_client.search.return_value = SearchResponse(
            results=[],
            total=0,
            retrieval_id="550e8400-e29b-41d4-a716-446655440003",
        )
        result = await engine.assemble(
            sessionId="s1",
            messages=[{"role": "user", "content": "search for orders context"}],
        )
        guide = result["systemPromptAddition"]
        assert "contexthub_feedback" in guide
        assert "retrieval_id" in guide
        assert "degraded mode" in guide
        assert "explicit tool-based search" in guide
        assert "returned by `read`" in guide
        assert "auto-recall" in guide


# ── Query extraction from instruction-wrapped messages ──────────────────


class TestExtractRecallQuery:

    def test_short_message_returns_full_text(self):
        msgs = [{"role": "user", "content": "What is the capital of France?"}]
        result = ContextHubContextEngine._extract_recall_query(msgs)
        assert result == "What is the capital of France?"

    def test_instruction_wrapped_extracts_question(self):
        instruction = (
            "You are answering a LoCoMo long-context memory evaluation question. "
            "Based on the seeded memory, please use the context to answer the "
            "following question accurately. Do not guess. If you do not have "
            "enough information, say 'insufficient information'.\n\n"
            "When did Caroline go to the LGBTQ support group?"
        )
        msgs = [{"role": "user", "content": instruction}]
        result = ContextHubContextEngine._extract_recall_query(msgs)
        assert result == "When did Caroline go to the LGBTQ support group?"

    def test_multiline_instruction_extracts_last_question(self):
        instruction = (
            "You are answering a LoCoMo long-context memory evaluation question. "
            "Based on the seeded memory, please use the context to answer the "
            "following question accurately. Do not guess or hallucinate.\n\n"
            "What was the date of the team meeting?"
        )
        assert len(instruction) > 200
        msgs = [{"role": "user", "content": instruction}]
        result = ContextHubContextEngine._extract_recall_query(msgs)
        assert result == "What was the date of the team meeting?"

    def test_no_question_mark_uses_last_line(self):
        instruction = (
            "You are an evaluation agent participating in a memory benchmark. "
            "Follow these instructions carefully and do not deviate from the "
            "provided context under any circumstances.\n\n"
            "Tell me about Caroline's schedule on March 15"
        )
        assert len(instruction) > 200
        msgs = [{"role": "user", "content": instruction}]
        result = ContextHubContextEngine._extract_recall_query(msgs)
        assert result == "Tell me about Caroline's schedule on March 15"

    def test_empty_messages_returns_none(self):
        result = ContextHubContextEngine._extract_recall_query([])
        assert result is None

    def test_no_user_message_returns_none(self):
        msgs = [{"role": "system", "content": "system prompt"}]
        result = ContextHubContextEngine._extract_recall_query(msgs)
        assert result is None

    def test_selects_last_user_message(self):
        msgs = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second question?"},
        ]
        result = ContextHubContextEngine._extract_recall_query(msgs)
        assert result == "second question?"


class TestExtractQuestionFromLongText:

    def test_finds_question_line(self):
        text = "Line one.\nLine two.\nWhat is the answer to this?"
        result = ContextHubContextEngine._extract_question_from_long_text(text)
        assert result == "What is the answer to this?"

    def test_finds_last_question_among_multiple(self):
        text = "Is this one? No.\nWhat about this second question?"
        result = ContextHubContextEngine._extract_question_from_long_text(text)
        assert result == "What about this second question?"

    def test_no_question_returns_last_line(self):
        text = "First line.\nSecond line.\nDescribe the event in detail"
        result = ContextHubContextEngine._extract_question_from_long_text(text)
        assert result == "Describe the event in detail"

    def test_empty_returns_none(self):
        result = ContextHubContextEngine._extract_question_from_long_text("")
        assert result is None


class TestLooksLikeUriOnly:

    def test_plain_uri(self):
        assert ContextHubContextEngine._looks_like_uri_only("ctx://agent/a/memories/m1") is True

    def test_uri_with_content(self):
        assert ContextHubContextEngine._looks_like_uri_only("ctx://a/m1 some text") is False

    def test_plain_text(self):
        assert ContextHubContextEngine._looks_like_uri_only("Caroline went to the group") is False


class TestAssembleRecallFormat:

    @pytest.mark.asyncio
    async def test_recall_includes_structured_source(self, engine, mock_client):
        mock_client.search.return_value = SearchResponse(
            results=[
                SearchResult(
                    uri="ctx://agent/a/memories/m1",
                    context_type=ContextType.MEMORY,
                    scope=Scope.AGENT,
                    score=0.92,
                    l0_content="short",
                    l1_content="Caroline attended the LGBTQ support group on March 15",
                    status=ContextStatus.ACTIVE,
                    version=1,
                )
            ],
            total=1,
            retrieval_id="550e8400-e29b-41d4-a716-446655440004",
        )
        result = await engine.assemble(
            sessionId="s1",
            messages=[{"role": "user", "content": "When did Caroline go to the LGBTQ support group?"}],
        )
        spa = result["systemPromptAddition"]
        assert "Caroline attended the LGBTQ support group on March 15" in spa
        assert "**Source**:" in spa
        assert "score:" in spa

    @pytest.mark.asyncio
    async def test_recall_skips_uri_only_content(self, engine, mock_client):
        mock_client.search.return_value = SearchResponse(
            results=[
                SearchResult(
                    uri="ctx://agent/a/memories/m1",
                    context_type=ContextType.MEMORY,
                    scope=Scope.AGENT,
                    score=0.9,
                    l0_content="ctx://agent/a/memories/m1",
                    l1_content=None,
                    status=ContextStatus.ACTIVE,
                    version=1,
                )
            ],
            total=1,
            retrieval_id="550e8400-e29b-41d4-a716-446655440005",
        )
        result = await engine.assemble(
            sessionId="s1",
            messages=[{"role": "user", "content": "query"}],
        )
        assert "Auto-Recall" not in result["systemPromptAddition"]


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
        mock_client.search.return_value = SearchResponse(
            results=[],
            total=0,
            retrieval_id="550e8400-e29b-41d4-a716-446655440006",
        )
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
