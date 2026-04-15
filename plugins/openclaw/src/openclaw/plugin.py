"""ContextHubContextEngine lifecycle methods for the OpenClaw plugin."""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

from contexthub_sdk import ContextHubClient

from .tools import TOOL_DEFINITIONS, dispatch

logger = logging.getLogger(__name__)

_MIN_CAPTURE_LENGTH = 80
_MAX_CAPTURE_SEGMENTS = 3
_MAX_CAPTURE_CHARS = 600
_ESTIMATED_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 4
_RECALL_HEADER = "## ContextHub Auto-Recall"
_TOOL_GUIDE = """\
## ContextHub Tools Guide
- When the user asks you to **remember**, **save**, or **note down** information, \
call `contexthub_store` with the content.
- When the user asks to **share** or **promote** a memory to a team, \
call `contexthub_promote` with the memory URI and target team name.
- To **list** stored memories or shared knowledge, call `ls` with a URI prefix \
(e.g. `ctx://agent/<agent_id>/memories` or `ctx://team/<team>/shared_knowledge`).
- To **search** for relevant context, call `grep` with a keyword or question.
- After you explicitly use context returned by `grep`, call `contexthub_feedback` \
with the `context_uri` and outcome.
- If you directly use a context returned by `read`, you may also call \
`contexthub_feedback` with the `context_uri`; this is degraded mode when no \
`retrieval_id` is available.
- When available, include `SearchResponse.retrieval_id` in `contexthub_feedback`; \
if missing, you may still report feedback in degraded mode.
- This feedback guidance applies only to explicit tool-based search usage, not \
to auto-recall text injected into the prompt."""
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_SEGMENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUESTION_SENTENCE_RE = re.compile(r"[^.!?\n]{10,}\?")
_LONG_MESSAGE_THRESHOLD = 200
_REUSABLE_HINTS = (
    "always",
    "endpoint",
    "header",
    "must",
    "never",
    "path",
    "requires",
    "return ",
    "returns",
    "token",
    "uri",
    "use ",
    "version",
)
_REUSABLE_MARKERS = ("`", "/api/", "ctx://", "http://", "https://", "mem://", "skill://", "x-")
_SKIP_PREFIXES = ("here is", "here's", "i can", "let me know", "we can")


class ContextHubContextEngine:
    """Python canonical implementation of the ContextHub context-engine plugin."""

    def __init__(self, client: ContextHubClient) -> None:
        self._client = client

    @property
    def info(self) -> dict[str, str]:
        return {
            "kind": "context-engine",
            "id": "contexthub",
            "name": "contexthub",
        }

    @property
    def tools(self) -> list[dict[str, Any]]:
        """JSON Schema definitions for the 8 MVP tools."""
        return TOOL_DEFINITIONS

    async def dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call. Returns a JSON string result."""
        return await dispatch(self._client, tool_name, args)

    async def ingest(
        self, *, sessionId: str, message: Any, isHeartbeat: bool = False
    ) -> dict[str, Any]:
        return {"ingested": False}

    async def ingestBatch(
        self, *, sessionId: str, messages: list[Any], isHeartbeat: bool = False
    ) -> dict[str, Any]:
        return {"ingested": False}

    async def assemble(
        self,
        *,
        sessionId: str,
        messages: list[dict[str, Any]],
        tokenBudget: int | None = None,
    ) -> dict[str, Any]:
        """Inject tool guide + auto-recall results via systemPromptAddition."""
        last_user = self._flatten_content(
            next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        ).strip()
        logger.info(
            "assemble_start session=%s messageCount=%d tokenBudget=%s lastUser=%r",
            sessionId, len(messages), tokenBudget, last_user[:120] if last_user else None,
        )

        message_tokens = self._estimate_message_tokens(messages)
        guide_tokens = self._estimate_text_tokens(_TOOL_GUIDE)

        recall_budget = None
        if tokenBudget is not None:
            recall_budget = max(tokenBudget - message_tokens - guide_tokens, 0)

        recall_text = await self._auto_recall(messages, max_tokens=recall_budget)
        recall_tokens = self._estimate_text_tokens(recall_text or "")

        addition_parts = [_TOOL_GUIDE]
        if recall_text:
            addition_parts.append(recall_text)
        system_addition = "\n\n".join(addition_parts)

        logger.info(
            "assemble_done session=%s hasRecall=%s recallTokens=%d totalTokens=%d",
            sessionId, recall_text is not None, recall_tokens,
            message_tokens + guide_tokens + recall_tokens,
        )

        return {
            "messages": messages,
            "estimatedTokens": message_tokens + guide_tokens + recall_tokens,
            "systemPromptAddition": system_addition,
        }

    async def _auto_recall(
        self, messages: list[dict[str, Any]], *, max_tokens: int | None
    ) -> str | None:
        query = self._extract_recall_query(messages)
        if not query:
            logger.debug("auto_recall: no user query found, skipping")
            return None
        if max_tokens is not None and max_tokens <= 0:
            logger.debug("auto_recall: no token budget remaining, skipping")
            return None

        logger.info("auto_recall_search query=%r max_tokens=%s", query[:120], max_tokens)

        try:
            resp = await self._client.search(query, top_k=5)
            logger.info(
                "auto_recall_hits hits=%d query=%r",
                len(resp.results), query[:80],
            )

            parts = []
            for i, result in enumerate(resp.results):
                content = self._normalize_whitespace(result.l1_content or result.l0_content or "")
                if not content or self._looks_like_uri_only(content):
                    logger.debug(
                        "auto_recall_skip idx=%d uri=%s reason=%s",
                        i, result.uri, "empty" if not content else "uri_only",
                    )
                    continue
                parts.append(f"- **Source**: `{result.uri}` (score: {result.score:.2f})\n  {content}")
                logger.debug(
                    "auto_recall_hit idx=%d uri=%s score=%.3f preview=%r",
                    i, result.uri, result.score, content[:100],
                )

            if not parts:
                logger.info("auto_recall: hits found but no usable content extracted")
                return None

            recall_text = (
                f"{_RECALL_HEADER}\n"
                "The following relevant context was retrieved from memory:\n\n"
                + "\n\n".join(parts)
            )
            if max_tokens is None:
                return recall_text
            return self._truncate_to_token_budget(recall_text, max_tokens)
        except Exception:
            logger.warning("Auto-recall failed, degrading gracefully", exc_info=True)
            return None

    @staticmethod
    def _looks_like_uri_only(text: str) -> bool:
        """Return True if the text is just a URI with no real factual content."""
        stripped = text.strip()
        return bool(
            stripped.startswith(("ctx://", "mem://", "skill://", "http://", "https://"))
            and " " not in stripped
        )

    @staticmethod
    def _extract_recall_query(messages: list[dict[str, Any]]) -> str | None:
        """Extract the actual user question from the last user message.

        For short messages the full text is used.  For long messages
        (typical of evaluation harnesses that wrap the real question
        inside instruction boilerplate) we try to isolate the actual
        question by scanning from the *end* of the text.
        """
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = ContextHubContextEngine._flatten_content(message.get("content", ""))
            content = content.strip()
            if not content:
                continue

            if len(content) <= _LONG_MESSAGE_THRESHOLD:
                return content

            extracted = ContextHubContextEngine._extract_question_from_long_text(content)
            query = extracted or content[-_LONG_MESSAGE_THRESHOLD:]
            logger.debug("extract_recall_query: long message (%d chars) → query=%r", len(content), query[:120])
            return query[:_LONG_MESSAGE_THRESHOLD]
        return None

    @staticmethod
    def _extract_question_from_long_text(text: str) -> str | None:
        """Best-effort extraction of the real question from instruction-wrapped text.

        Tries, in order:
        1. Last line that ends with '?' (most reliable for QA evaluation)
        2. Last regex-matched question sentence anywhere in the text
        3. Last non-trivial line (likely the actual question placed at the end)
        """
        lines = text.strip().splitlines()
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.endswith("?") and len(stripped) > 10:
                return stripped

        matches = _QUESTION_SENTENCE_RE.findall(text)
        if matches:
            last_q = matches[-1].strip()
            if len(last_q) > 10:
                return last_q

        for line in reversed(lines):
            stripped = line.strip()
            if len(stripped) > 10:
                return stripped

        return None

    async def afterTurn(
        self,
        *,
        sessionId: str,
        messages: list[dict[str, Any]],
        prePromptMessageCount: int,
    ) -> None:
        """Auto-capture conservative reusable facts from the latest turn.

        Disabled when CONTEXTHUB_AUTO_CAPTURE=off (case-insensitive).
        """
        if os.getenv("CONTEXTHUB_AUTO_CAPTURE", "on").lower() in ("off", "false", "0", "no"):
            return
        snippet = self._extract_capturable(messages, prePromptMessageCount)
        if not snippet:
            return
        try:
            await self._client.memory.add(content=snippet, tags=["auto-capture"])
        except Exception:
            logger.warning("Auto-capture write failed", exc_info=True)

    @classmethod
    def _extract_capturable(
        cls, messages: list[dict[str, Any]], prePromptMessageCount: int
    ) -> str | None:
        turn_messages = messages[prePromptMessageCount:]
        assistant_text = ""
        for message in reversed(turn_messages):
            if message.get("role") == "assistant":
                assistant_text = cls._flatten_content(message.get("content", ""))
                break

        assistant_text = assistant_text.strip()
        if len(assistant_text) < _MIN_CAPTURE_LENGTH:
            return None

        normalized_text = _CODE_BLOCK_RE.sub(" ", assistant_text)
        candidates: list[str] = []
        seen: set[str] = set()
        for raw_segment in _SEGMENT_SPLIT_RE.split(normalized_text):
            segment = cls._normalize_capture_segment(raw_segment)
            if not segment or segment in seen:
                continue
            if not cls._looks_reusable(segment):
                continue
            seen.add(segment)
            candidates.append(segment)
            if len(candidates) >= _MAX_CAPTURE_SEGMENTS:
                break

        if not candidates:
            return None

        return "\n".join(candidates)[:_MAX_CAPTURE_CHARS].rstrip()

    @staticmethod
    def _normalize_capture_segment(text: str) -> str:
        text = text.strip()
        text = text.lstrip("-*0123456789. ").strip()
        return ContextHubContextEngine._normalize_whitespace(text)

    @staticmethod
    def _looks_reusable(text: str) -> bool:
        if len(text) < 24 or len(text) > 240:
            return False

        lower = text.lower()
        if lower.startswith(_SKIP_PREFIXES):
            return False

        if any(marker in lower for marker in _REUSABLE_MARKERS):
            return True

        return any(hint in lower for hint in _REUSABLE_HINTS)

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _flatten_content(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [cls._flatten_content(part) for part in content]
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
            if "content" in content:
                return cls._flatten_content(content["content"])
        return ""

    @classmethod
    def _estimate_message_tokens(cls, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            content = cls._flatten_content(message.get("content", ""))
            total += _MESSAGE_OVERHEAD_TOKENS + cls._estimate_text_tokens(content)
        return total

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        stripped = text.strip()
        if not stripped:
            return 0
        return max(1, math.ceil(len(stripped) / _ESTIMATED_CHARS_PER_TOKEN))

    @staticmethod
    def _truncate_to_token_budget(text: str, max_tokens: int) -> str | None:
        if max_tokens <= 0:
            return None

        max_chars = max_tokens * _ESTIMATED_CHARS_PER_TOKEN
        if max_chars <= len(_RECALL_HEADER) + 8:
            return None

        stripped = text.strip()
        if len(stripped) <= max_chars:
            return stripped

        cutoff = max_chars - 3
        truncated = stripped[:cutoff]
        if " " in truncated:
            truncated = truncated.rsplit(" ", 1)[0]
        truncated = truncated.rstrip(" \n,;:")
        if len(truncated) <= len(_RECALL_HEADER) + 8:
            return None
        return truncated + "..."

    async def compact(
        self,
        *,
        sessionId: str,
        sessionFile: Any = None,
        tokenBudget: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """ContextHub does not own compaction."""
        return {"compacted": False}

    async def dispose(self) -> None:
        """Clean up resources."""
        await self._client.aclose()
