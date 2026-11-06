from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

from bootstrap import bootstrap_repo_paths

bootstrap_repo_paths()

from contexthub_sdk import ContextHubClient

from tools import TOOL_DEFINITIONS, dispatch

logger = logging.getLogger(__name__)

_MIN_CAPTURE_LENGTH = 80
_MAX_CAPTURE_SEGMENTS = 3
_MAX_CAPTURE_CHARS = 600
_ESTIMATED_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 4
_RECALL_HEADER = "## ContextHub Auto-Recall"
_TOOL_GUIDE = """\
## ContextHub Tools Guide
- When the user asks you to remember, save, or note down information, call `contexthub_store`.
- When the user asks to share or promote a memory to a team, call `contexthub_promote`.
- To list stored memories or shared knowledge, call `ls`.
- To search for relevant context, call `grep`.
"""
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_SEGMENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
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
_QUESTION_NOISE = (
    "应该怎么查",
    "怎么查",
    "如何查",
    "如何查询",
    "应该如何查询",
    "应该如何查",
    "应该怎么做",
    "怎么做",
    "是什么",
    "怎么办",
)


class ContextHubJiuwenEngine:
    def __init__(self, client: ContextHubClient) -> None:
        self._client = client

    @property
    def info(self) -> dict[str, str]:
        return {"kind": "context-engine", "id": "contexthub_jiuwen", "name": "contexthub_jiuwen"}

    @property
    def tools(self) -> list[dict[str, Any]]:
        return TOOL_DEFINITIONS

    async def dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        return await dispatch(self._client, tool_name, args)

    async def ingest(self, *, sessionId: str, message: Any, isHeartbeat: bool = False) -> dict[str, Any]:
        return {"ingested": False}

    async def ingestBatch(self, *, sessionId: str, messages: list[Any], isHeartbeat: bool = False) -> dict[str, Any]:
        return {"ingested": False}

    async def assemble(
        self,
        *,
        sessionId: str,
        messages: list[dict[str, Any]],
        tokenBudget: int | None = None,
    ) -> dict[str, Any]:
        message_tokens = self._estimate_message_tokens(messages)
        guide_tokens = self._estimate_text_tokens(_TOOL_GUIDE)
        recall_budget = None
        if tokenBudget is not None:
            recall_budget = max(tokenBudget - message_tokens - guide_tokens, 0)
        recall_text = await self._auto_recall(messages, max_tokens=recall_budget)
        recall_tokens = self._estimate_text_tokens(recall_text or "")

        parts = [_TOOL_GUIDE]
        if recall_text:
            parts.append(recall_text)
        return {
            "messages": messages,
            "estimatedTokens": message_tokens + guide_tokens + recall_tokens,
            "systemPromptAddition": "\n\n".join(parts),
        }

    async def afterTurn(
        self,
        *,
        sessionId: str,
        messages: list[dict[str, Any]],
        prePromptMessageCount: int,
    ) -> None:
        if os.getenv("CONTEXTHUB_AUTO_CAPTURE", "on").lower() in ("off", "false", "0", "no"):
            return
        snippet = self._extract_capturable(messages, prePromptMessageCount)
        if not snippet:
            return
        try:
            await self._client.memory.add(content=snippet, tags=["auto-capture"])
        except Exception:
            logger.warning("Auto-capture write failed", exc_info=True)

    async def compact(
        self,
        *,
        sessionId: str,
        sessionFile: Any = None,
        tokenBudget: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return {"compacted": False}

    async def dispose(self) -> None:
        await self._client.aclose()

    async def _auto_recall(self, messages: list[dict[str, Any]], *, max_tokens: int | None) -> str | None:
        query = self._extract_recall_query(messages)
        if not query or (max_tokens is not None and max_tokens <= 0):
            return None
        try:
            parts: list[str] = []
            seen: set[str] = set()
            for candidate in self._query_candidates(query):
                resp = await self._client.search(candidate, top_k=3)
                for result in resp.results:
                    content = self._normalize_whitespace(result.l1_content or result.l0_content or "")
                    if content and result.uri not in seen:
                        seen.add(result.uri)
                        parts.append(f"[{result.uri}] {content}")
                if parts:
                    break
            if not parts:
                return None
            text = f"{_RECALL_HEADER}\n\n" + "\n\n".join(parts)
            if max_tokens is None:
                return text
            return self._truncate_to_token_budget(text, max_tokens)
        except Exception:
            logger.warning("Auto-recall failed", exc_info=True)
            return None

    @classmethod
    def _query_candidates(cls, query: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            normalized = cls._normalize_whitespace(value)
            if normalized and normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

        add(query)
        simplified = query.strip().strip("？?！!。.")
        add(simplified)
        trimmed = simplified
        for noise in _QUESTION_NOISE:
            trimmed = trimmed.replace(noise, " ")
        add(trimmed)
        chunks = re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}", trimmed)
        if chunks:
            add(" ".join(chunks))
            add("".join(chunk for chunk in chunks if re.search(r"[\u4e00-\u9fff]", chunk)))
        return candidates

    @classmethod
    def _extract_capturable(cls, messages: list[dict[str, Any]], prePromptMessageCount: int) -> str | None:
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

    @classmethod
    def _extract_recall_query(cls, messages: list[dict[str, Any]]) -> str | None:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = cls._flatten_content(message.get("content", "")).strip()
            if content:
                return content[:200]
        return None

    @staticmethod
    def _normalize_capture_segment(text: str) -> str:
        text = text.strip()
        text = text.lstrip("-*0123456789. ").strip()
        return ContextHubJiuwenEngine._normalize_whitespace(text)

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
