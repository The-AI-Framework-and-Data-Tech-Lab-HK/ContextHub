from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.llm.chat_client import BaseChatClient
from contexthub.models.document import DocumentSection

from .result import MAX_SNIPPET_CHARS, LongDocRetrievalResult

logger = logging.getLogger(__name__)

TREE_MAX_DEPTH = 8
TREE_SELECTION_PROMPT_CHAR_LIMIT = 12000
TREE_LEAF_TOKEN_TARGET = 2000
TREE_PARENT_STOP_TOKEN_LIMIT = 4000
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "what",
    "when", "where", "why", "with",
}


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 2}


def _normalize_for_match(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _keyword_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _clamp_snippet(text: str, start: int, end: int, max_chars: int) -> tuple[str, tuple[int, int]] | None:
    start = max(0, min(start, len(text)))
    end = max(0, min(end, len(text)))
    if start >= end:
        return None
    bounded_end = min(end, start + max_chars)
    snippet = text[start:bounded_end]
    if not snippet:
        return None
    return snippet, (start, start + len(snippet))


class TreeRetriever:
    def __init__(self, chat_client: BaseChatClient, *, max_snippet_chars: int = MAX_SNIPPET_CHARS):
        self._chat_client = chat_client
        self._max_snippet_chars = max_snippet_chars

    async def retrieve(
        self,
        db: ScopedRepo,
        query: str,
        context_id: UUID,
        uri: str,
        file_path: str,
        *,
        base_score: float = 0.0,
    ) -> list[LongDocRetrievalResult]:
        sections = await self._load_sections(db, context_id)
        if not sections:
            return []

        text = self._read_extracted_text(file_path, uri)
        if text is None:
            return []

        selected = await self._select_section(query, sections, text)
        if selected is None:
            return []

        start = selected.start_offset if selected.start_offset is not None else 0
        end = selected.end_offset if selected.end_offset is not None else len(text)
        clipped = self._focus_snippet(text, start, end, query)
        if clipped is None:
            return []
        snippet, snippet_offset = clipped

        return [
            LongDocRetrievalResult(
                context_id=context_id,
                uri=uri,
                strategy="tree",
                section_id=selected.section_id,
                snippet=snippet,
                snippet_offset=snippet_offset,
                relevance_score=float(base_score) * self._score_multiplier(query, selected, sections),
            )
        ]

    async def _load_sections(self, db: ScopedRepo, context_id: UUID) -> list[DocumentSection]:
        rows = await db.fetch(
            """
            SELECT section_id, context_id, parent_id, node_id, title, depth,
                   start_offset, end_offset, summary, token_count, account_id, created_at
            FROM document_sections
            WHERE context_id = $1
            ORDER BY depth ASC, parent_id NULLS FIRST, start_offset ASC, section_id ASC
            """,
            context_id,
        )
        return [DocumentSection.model_validate(dict(row)) for row in rows]

    async def _select_section(
        self,
        query: str,
        sections: list[DocumentSection],
        text: str,
    ) -> DocumentSection | None:
        by_parent: dict[int | None, list[DocumentSection]] = defaultdict(list)
        for section in sections:
            by_parent[section.parent_id].append(section)

        roots = by_parent.get(None, [])
        if not roots:
            return None

        current = roots[0] if len(roots) == 1 else await self._pick_candidate_async(query, roots, text)
        if current is None:
            return None

        depth = 0
        while current is not None and depth < TREE_MAX_DEPTH:
            children = by_parent.get(current.section_id, [])
            if not children:
                return current
            if (current.token_count or 0) <= TREE_LEAF_TOKEN_TARGET:
                return current
            next_candidate = await self._pick_candidate_async(query, children, text)
            if next_candidate is None:
                return current
            if self._should_stop_at_current(query, current, next_candidate, text):
                return current
            current = next_candidate
            depth += 1

        return current

    async def _llm_pick(
        self,
        query: str,
        candidates: list[DocumentSection],
        text: str,
    ) -> int | None:
        prompt = self._build_selection_prompt(query, candidates, text)
        try:
            raw = (await self._chat_client.complete(prompt, max_tokens=32)).strip()
        except Exception:
            logger.warning("Tree retriever LLM selection failed", exc_info=True)
            return None
        if not raw:
            return None
        match = re.search(r"\d+", raw)
        if match is None:
            return None
        return int(match.group(0))

    def _build_selection_prompt(
        self,
        query: str,
        candidates: list[DocumentSection],
        text: str,
    ) -> str:
        header = (
            "Pick the single best section_id for the query.\n"
            "Return only the numeric section_id.\n"
            f"Query: {query}\n"
            "Candidates:\n"
        )
        parts: list[str] = []
        remaining = max(0, TREE_SELECTION_PROMPT_CHAR_LIMIT - len(header))
        for section in candidates:
            line = (
                f"- section_id={section.section_id}; title={section.title[:200]!r}; "
                f"summary={(section.summary or '')[:300]!r}; "
                f"preview={self._section_preview(text, section)[:240]!r}; "
                f"token_count={section.token_count or 0}\n"
            )
            if len(line) > remaining:
                break
            parts.append(line)
            remaining -= len(line)
        return (header + "".join(parts))[:TREE_SELECTION_PROMPT_CHAR_LIMIT]

    def _section_preview(self, text: str, section: DocumentSection, *, limit: int = 400) -> str:
        start = max(0, min(section.start_offset or 0, len(text)))
        end = max(start, min(section.end_offset or start, len(text)))
        focused = self._best_window(text, start, end, query="", max_chars=limit)
        if focused is not None:
            return focused[0]
        return text[start : min(end, start + limit)]

    def _section_score(
        self,
        query: str,
        section: DocumentSection,
        text: str,
    ) -> tuple[int, int, int, int, int]:
        query_tokens = _tokenize(query)
        preview = self._best_window(
            text,
            max(0, min(section.start_offset or 0, len(text))),
            max(0, min(section.end_offset or 0, len(text))),
            query=query,
            max_chars=400,
        )
        preview_text = preview[0] if preview is not None else self._section_preview(text, section)
        title_summary_tokens = _tokenize(f"{section.title} {section.summary or ''}")
        preview_tokens = _tokenize(preview_text)
        union_overlap = len(query_tokens & (title_summary_tokens | preview_tokens))
        preview_overlap = len(query_tokens & preview_tokens)
        title_summary_overlap = len(query_tokens & title_summary_tokens)
        phrase_haystack = _normalize_for_match(
            f"{section.title} {section.summary or ''} {preview_text}"
        )
        phrase_bonus = 1 if _normalize_for_match(query) in phrase_haystack else 0
        token_count = section.token_count or TREE_LEAF_TOKEN_TARGET
        proximity = -abs(token_count - TREE_LEAF_TOKEN_TARGET)
        return (
            phrase_bonus,
            union_overlap,
            preview_overlap,
            title_summary_overlap,
            proximity,
        )

    def _heuristic_pick(
        self,
        query: str,
        candidates: list[DocumentSection],
        text: str,
    ) -> DocumentSection:
        def sort_key(section: DocumentSection) -> tuple[int, int, int, int, int, int]:
            phrase_bonus, union_overlap, preview_overlap, title_summary_overlap, proximity = (
                self._section_score(query, section, text)
            )
            return (
                phrase_bonus,
                union_overlap,
                preview_overlap,
                title_summary_overlap,
                section.depth,
                proximity,
            )

        return max(candidates, key=sort_key)

    def _should_stop_at_current(
        self,
        query: str,
        current: DocumentSection,
        child: DocumentSection,
        text: str,
    ) -> bool:
        if (current.token_count or TREE_PARENT_STOP_TOKEN_LIMIT + 1) > TREE_PARENT_STOP_TOKEN_LIMIT:
            return False
        current_score = self._section_score(query, current, text)
        child_score = self._section_score(query, child, text)
        current_preview_overlap = current_score[2]
        child_preview_overlap = child_score[2]
        if current_preview_overlap == 0:
            return False
        return current_score >= child_score and current_preview_overlap >= child_preview_overlap

    def _focus_snippet(
        self,
        text: str,
        start: int,
        end: int,
        query: str,
    ) -> tuple[str, tuple[int, int]] | None:
        focused = self._best_window(text, start, end, query=query, max_chars=self._max_snippet_chars)
        if focused is not None:
            return focused
        return _clamp_snippet(text, start, end, self._max_snippet_chars)

    def _best_window(
        self,
        text: str,
        start: int,
        end: int,
        *,
        query: str,
        max_chars: int,
    ) -> tuple[str, tuple[int, int]] | None:
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        if start >= end:
            return None

        tokens = _keyword_tokens(query)
        if not tokens:
            return None

        section_text = text[start:end]
        lowered = section_text.lower()
        hits: list[int] = []
        for token in tokens:
            cursor = lowered.find(token)
            while cursor >= 0:
                hits.append(cursor)
                cursor = lowered.find(token, cursor + 1)
        if not hits:
            return None

        candidates: list[tuple[int, int]] = []
        half_window = max_chars // 2
        for hit in sorted(set(hits)):
            for window_start in (
                max(0, hit - half_window),
                max(0, hit - max_chars // 3),
                max(0, min(hit, max(0, len(section_text) - max_chars))),
            ):
                window_end = min(len(section_text), max(window_start + 1, window_start + max_chars))
                if window_start <= hit < window_end:
                    candidates.append((window_start, window_end))
        if not candidates:
            return None

        exact_phrase = _normalize_for_match(query)

        def score(window: tuple[int, int]) -> tuple[int, int, float, int]:
            window_start, window_end = window
            snippet = section_text[window_start:window_end]
            snippet_lower = snippet.lower()
            snippet_norm = _normalize_for_match(snippet)
            coverage = sum(1 for token in tokens if token in snippet_lower)
            density = sum(snippet_lower.count(token) for token in tokens) / max(1, len(snippet))
            phrase_bonus = 1 if exact_phrase and exact_phrase in snippet_norm else 0
            return (
                phrase_bonus,
                coverage,
                density,
                -window_start,
            )

        best_start, best_end = max(set(candidates), key=score)
        absolute_start = start + best_start
        absolute_end = start + best_end
        return text[absolute_start:absolute_end], (absolute_start, absolute_end)

    def _read_extracted_text(self, file_path: str, uri: str) -> str | None:
        try:
            return (Path(file_path) / "extracted.txt").read_text(encoding="utf-8")
        except OSError:
            logger.warning("Tree retriever could not read extracted.txt for %s", uri, exc_info=True)
            return None

    def _score_multiplier(
        self,
        query: str,
        section: DocumentSection,
        sections: list[DocumentSection],
    ) -> float:
        query_tokens = _tokenize(query)
        node_tokens = _tokenize(f"{section.title} {section.summary or ''}")
        overlap_ratio = 0.0
        if query_tokens:
            overlap_ratio = len(query_tokens & node_tokens) / len(query_tokens)

        max_depth = max((item.depth for item in sections), default=0)
        depth_bonus = 0.1 * (section.depth / max_depth) if max_depth else 0.0
        leaf_bonus = 0.05 if (section.token_count or TREE_LEAF_TOKEN_TARGET + 1) <= TREE_LEAF_TOKEN_TARGET else 0.0
        multiplier = 0.8 + min(0.25, overlap_ratio * 0.2) + depth_bonus + leaf_bonus
        return max(0.8, min(1.2, multiplier))

    async def _pick_candidate_async(
        self,
        query: str,
        candidates: list[DocumentSection],
        text: str,
    ) -> DocumentSection | None:
        if not candidates:
            return None
        llm_section_id = await self._llm_pick(query, candidates, text)
        if llm_section_id is not None:
            for candidate in candidates:
                if candidate.section_id == llm_section_id:
                    return candidate
        return self._heuristic_pick(query, candidates, text)
