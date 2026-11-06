from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path

from contexthub.db.repository import ScopedRepo
from contexthub.retrieval.rerank import bm25_scores

from .result import MAX_SNIPPET_CHARS, LongDocRetrievalResult
from .ranges import document_ranges, enclosing_section

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "what",
    "when", "where", "why", "with",
}


def _tokenize(text: str) -> list[str]:
    """Whole word/number runs, with individual Han characters for unspaced text.

    This deterministic tokenizer does not stem English words or infer Chinese
    word boundaries. Both the query and original fragments use this same rule.
    """
    return re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]|[^\W\u3400-\u4dbf\u4e00-\u9fff]+", text.lower())


class KeywordRetriever:
    def __init__(self, *, max_snippet_chars: int = MAX_SNIPPET_CHARS):
        self._max_snippet_chars = max_snippet_chars

    async def retrieve(
        self,
        db: ScopedRepo,
        query: str,
        doc_contexts: list[dict],
    ) -> list[LongDocRetrievalResult]:
        # Preserve the existing query stopwords and deduplication. Query and
        # source use the same tokenizer, so "once" cannot match "concepts".
        query_tokens = list(dict.fromkeys(
            token for token in _tokenize(query) if token not in _STOPWORDS))
        if not doc_contexts or not query_tokens:
            return []

        fragments: list[LongDocRetrievalResult] = []
        for doc in doc_contexts:
            fragments.extend(await self._document_fragments(db, doc))
        # All ranges of all supplied (already access-filtered) documents form
        # the corpus, including zero-hit ranges, before any query filtering.
        scores = bm25_scores(query_tokens, [_tokenize(f.snippet) for f in fragments])
        results = [replace(fragment, relevance_score=score)
                   for fragment, score in zip(fragments, scores) if score > 0]
        return sorted(results, key=lambda item: item.relevance_score, reverse=True)

    async def _document_fragments(
        self, db: ScopedRepo, doc: dict,
    ) -> list[LongDocRetrievalResult]:
        file_path = doc.get("file_path")
        if not file_path:
            return []
        try:
            text = (Path(file_path) / "extracted.txt").read_text(encoding="utf-8")
        except OSError:
            logger.warning("Keyword retriever skipped unreadable extracted.txt for %s", doc.get("uri"))
            return []

        sections = [dict(row) for row in await db.fetch(
            """SELECT section_id, parent_id, depth, start_offset, end_offset
                 FROM document_sections WHERE context_id = $1
                ORDER BY start_offset, depth, section_id""",
            doc["id"],
        )]
        results = []
        for start, end in document_ranges(text, sections, self._max_snippet_chars):
            section = enclosing_section(sections, start, end)
            results.append(LongDocRetrievalResult(
                context_id=doc["id"], uri=doc["uri"], strategy="keyword",
                section_id=section["section_id"] if section else None,
                snippet=text[start:end], snippet_offset=(start, end), relevance_score=0.0))
        return results
