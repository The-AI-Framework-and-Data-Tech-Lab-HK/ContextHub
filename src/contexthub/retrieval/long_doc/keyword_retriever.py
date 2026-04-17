from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from contexthub.db.repository import ScopedRepo
from contexthub.llm.chat_client import BaseChatClient

from .result import MAX_SNIPPET_CHARS, LongDocRetrievalResult

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "what",
    "when", "where", "why", "with",
}


def _tokenize(text: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        if len(token) < 3 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


class KeywordRetriever:
    def __init__(
        self,
        chat_client: BaseChatClient,
        *,
        rg_path: str = "rg",
        max_snippet_chars: int = MAX_SNIPPET_CHARS,
    ):
        self._chat_client = chat_client
        self._rg = rg_path
        self._max_snippet_chars = max_snippet_chars

    async def retrieve(
        self,
        db: ScopedRepo,
        query: str,
        doc_contexts: list[dict],
    ) -> list[LongDocRetrievalResult]:
        del db
        if not doc_contexts:
            return []
        if not self._rg_available():
            logger.warning("Keyword retriever skipped because ripgrep is unavailable")
            return []

        keyword_groups = await self._extract_keywords(query)
        if not keyword_groups:
            return []

        results: list[LongDocRetrievalResult] = []
        for doc in doc_contexts:
            result = await self._retrieve_for_document(doc, keyword_groups)
            if result is not None:
                results.append(result)
        results.sort(key=lambda item: item.relevance_score, reverse=True)
        return results

    async def _extract_keywords(self, query: str) -> list[list[str]]:
        baseline = self._baseline_keyword_groups(query)
        prompt = (
            "Extract 1 to 4 keyword groups for searching a document.\n"
            "Return JSON only as an array of arrays of lowercase keywords.\n"
            f"Query: {query}"
        )
        try:
            raw = (await self._chat_client.complete(prompt, max_tokens=128)).strip()
        except Exception:
            return baseline
        parsed = self._parse_keyword_groups(raw)
        return parsed or baseline

    def _baseline_keyword_groups(self, query: str) -> list[list[str]]:
        tokens = _tokenize(query)
        if not tokens:
            return []
        groups: list[list[str]] = []
        if len(tokens) >= 2:
            groups.append(tokens[:2])
        for token in tokens[:4]:
            single = [token]
            if single not in groups:
                groups.append(single)
        return groups

    def _parse_keyword_groups(self, raw: str) -> list[list[str]]:
        if not raw:
            return []
        match = re.search(r"\[[\s\S]*\]", raw)
        if match is None:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        groups: list[list[str]] = []
        if not isinstance(payload, list):
            return []
        for item in payload:
            if not isinstance(item, list):
                continue
            group = [str(token).strip().lower() for token in item if str(token).strip()]
            group = [token for token in group if len(token) >= 2]
            if group and group not in groups:
                groups.append(group)
        return groups

    def _rg_available(self) -> bool:
        return shutil.which(self._rg) is not None or Path(self._rg).exists()

    async def _retrieve_for_document(
        self,
        doc: dict,
        keyword_groups: list[list[str]],
    ) -> LongDocRetrievalResult | None:
        file_path = doc.get("file_path")
        if not file_path:
            return None
        extracted_path = Path(file_path) / "extracted.txt"

        try:
            text = extracted_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Keyword retriever skipped unreadable extracted.txt for %s", doc.get("uri"))
            return None

        hit_positions = await self._collect_hit_positions(extracted_path, text, keyword_groups)
        if not hit_positions:
            return None

        windows = await self._monte_carlo_sample(text, hit_positions)
        if not windows:
            return None

        best_snippet, best_offset, best_score = self._pick_best_window(text, windows, keyword_groups)
        base_score = float(doc.get("_rerank_score", doc.get("cosine_similarity", 0.0)))
        return LongDocRetrievalResult(
            context_id=doc["id"],
            uri=doc["uri"],
            strategy="keyword",
            section_id=None,
            snippet=best_snippet,
            snippet_offset=best_offset,
            relevance_score=base_score * best_score,
        )

    async def _collect_hit_positions(
        self,
        extracted_path: Path,
        text: str,
        keyword_groups: list[list[str]],
    ) -> list[int]:
        positions: set[int] = set()
        line_offsets = self._line_offsets(text)
        line_byte_offsets = self._line_byte_offsets(text)
        byte_to_char = self._byte_to_char_offsets(text)
        for group in keyword_groups:
            pattern = "|".join(re.escape(token) for token in group)
            if not pattern:
                continue
            group_positions = await self._run_rg(
                extracted_path,
                pattern,
                line_offsets,
                line_byte_offsets,
                byte_to_char,
                text,
                group,
            )
            positions.update(group_positions)
        return sorted(positions)

    async def _run_rg(
        self,
        extracted_path: Path,
        pattern: str,
        line_offsets: list[int],
        line_byte_offsets: list[int],
        byte_to_char: dict[int, int],
        text: str,
        group: list[str],
    ) -> list[int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._rg,
                "--json",
                "-n",
                "-i",
                pattern,
                str(extracted_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.warning("Keyword retriever skipped because ripgrep is unavailable")
            return []

        stdout, _stderr = await proc.communicate()
        if proc.returncode == 1:
            return []
        if proc.returncode not in (0, 1):
            logger.warning("Keyword retriever rg failed for %s with exit code %s", extracted_path, proc.returncode)
            return []

        positions: list[int] = []
        for raw_line in stdout.decode("utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            line_number = data.get("line_number")
            absolute_offset = data.get("absolute_offset")
            line_text = ((data.get("lines") or {}).get("text")) or ""
            submatches = data.get("submatches") or []
            if isinstance(line_number, int) and 1 <= line_number <= len(line_offsets):
                base_offset = line_offsets[line_number - 1]
                base_byte_offset = line_byte_offsets[line_number - 1]
            elif isinstance(absolute_offset, int):
                base_offset = byte_to_char.get(absolute_offset, 0)
                base_byte_offset = absolute_offset
            else:
                base_offset = 0
                base_byte_offset = 0

            if submatches:
                for submatch in submatches:
                    start = submatch.get("start")
                    if isinstance(start, int):
                        char_pos = byte_to_char.get(base_byte_offset + start)
                        if char_pos is not None:
                            positions.append(char_pos)
                continue

            lowered = line_text.lower()
            for token in group:
                cursor = lowered.find(token.lower())
                while cursor >= 0:
                    positions.append(base_offset + cursor)
                    cursor = lowered.find(token.lower(), cursor + 1)

        bounded_positions = [pos for pos in positions if 0 <= pos < len(text)]
        return bounded_positions

    def _line_offsets(self, text: str) -> list[int]:
        offsets: list[int] = []
        cursor = 0
        for line in text.splitlines(keepends=True):
            offsets.append(cursor)
            cursor += len(line)
        if not offsets:
            offsets.append(0)
        return offsets

    def _line_byte_offsets(self, text: str) -> list[int]:
        offsets: list[int] = []
        cursor = 0
        for line in text.splitlines(keepends=True):
            offsets.append(cursor)
            cursor += len(line.encode("utf-8"))
        if not offsets:
            offsets.append(0)
        return offsets

    def _byte_to_char_offsets(self, text: str) -> dict[int, int]:
        offsets = {0: 0}
        cursor = 0
        for index, char in enumerate(text, start=1):
            cursor += len(char.encode("utf-8"))
            offsets[cursor] = index
        return offsets

    async def _monte_carlo_sample(
        self,
        text: str,
        hit_positions: list[int],
        *,
        window_size: int = 2000,
        n_samples: int = 5,
    ) -> list[tuple[str, tuple[int, int]]]:
        if not text or not hit_positions:
            return []

        windows: list[tuple[int, int]] = []
        for hit in sorted(hit_positions):
            starts = {
                max(0, min(hit, len(text))),
                max(0, hit - window_size // 2),
                max(0, hit - window_size // 3),
            }
            for start in starts:
                end = min(len(text), max(hit + 1, start + min(window_size, self._max_snippet_chars)))
                if start < end and start <= hit < end:
                    windows.append((start, end))

        merged = self._merge_windows(windows, max_window_chars=self._max_snippet_chars)
        samples: list[tuple[str, tuple[int, int]]] = []
        for start, end in merged[:n_samples]:
            snippet = text[start:end][: self._max_snippet_chars]
            if snippet:
                samples.append((snippet, (start, start + len(snippet))))
        return samples

    def _merge_windows(
        self,
        windows: list[tuple[int, int]],
        *,
        max_window_chars: int,
    ) -> list[tuple[int, int]]:
        if not windows:
            return []
        normalized = sorted(set(windows))
        merged: list[tuple[int, int]] = [normalized[0]]
        for start, end in normalized[1:]:
            prev_start, prev_end = merged[-1]
            merged_end = max(prev_end, end)
            if start <= prev_end and merged_end - prev_start <= max_window_chars:
                merged[-1] = (prev_start, merged_end)
            else:
                merged.append((start, end))
        return merged

    def _pick_best_window(
        self,
        text: str,
        windows: list[tuple[str, tuple[int, int]]],
        keyword_groups: list[list[str]],
    ) -> tuple[str, tuple[int, int], float]:
        all_keywords = {token for group in keyword_groups for token in group}

        def score(window: tuple[str, tuple[int, int]]) -> tuple[float, float]:
            snippet, (start, end) = window
            lowered = snippet.lower()
            coverage = sum(1 for token in all_keywords if token in lowered)
            density = sum(lowered.count(token) for token in all_keywords) / max(1, end - start)
            quality = 0.8 + min(0.3, coverage * 0.05 + density * 5)
            return quality, float(coverage)

        best = max(windows, key=score)
        snippet, offset = best
        quality, _ = score(best)
        return snippet, offset, min(1.2, max(0.8, quality))
