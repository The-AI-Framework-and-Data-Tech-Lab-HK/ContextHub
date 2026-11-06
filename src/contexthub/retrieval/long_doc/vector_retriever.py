"""Search original chunks independently of L0, using the already embedded query."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .result import LongDocRetrievalResult

logger = logging.getLogger(__name__)


_VECTOR_ROWS = """SELECT e.context_id, e.context_version, e.section_id,
                    e.start_offset, e.end_offset, e.source_sha256,
                    1 - (e.embedding <=> $1::vector) AS similarity
                FROM document_chunk_embeddings e JOIN contexts c ON c.id = e.context_id
                WHERE e.account_id = current_setting('app.account_id')
                  AND c.account_id = current_setting('app.account_id')
                  AND e.context_id = ANY($2::uuid[]) AND e.context_version = c.version"""


class ChunkVectorRetriever:
    async def retrieve(self, db, query_embedding, documents: list[dict], top_k: int):
        if query_embedding is None or not documents:
            return []
        rows = await db.fetch(
            _VECTOR_ROWS + """
                ORDER BY e.embedding <=> $1::vector, e.context_id, e.start_offset
                LIMIT $3""",
            "[" + ",".join(str(value) for value in query_embedding) + "]",
            [doc["id"] for doc in documents], top_k)
        return self._materialize(rows, documents)

    async def score_candidates(self, db, query_embedding, documents: list[dict], candidates):
        """Score only requested existing ranges, without a top-k cutoff or embedding call."""
        if query_embedding is None or not documents or not candidates:
            return []
        rows = await db.fetch(
            _VECTOR_ROWS + """
                AND (e.context_id, e.start_offset, e.end_offset) IN (
                    SELECT * FROM unnest($3::uuid[], $4::int[], $5::int[]))
                ORDER BY e.context_id, e.start_offset""",
            "[" + ",".join(str(value) for value in query_embedding) + "]",
            [doc["id"] for doc in documents],
            [candidate.context_id for candidate in candidates],
            [candidate.snippet_offset[0] for candidate in candidates],
            [candidate.snippet_offset[1] for candidate in candidates])
        return self._materialize(rows, documents)

    @staticmethod
    def _materialize(rows, documents):
        by_id = {doc["id"]: doc for doc in documents}
        sources = {}
        results = []
        for row in rows:
            doc = by_id[row["context_id"]]
            if doc["version"] != row["context_version"]:
                continue
            if doc["id"] not in sources:
                # Caller filtered ACL before file I/O; common search checks again before delivery.
                try:
                    source = (Path(doc["file_path"]) / "extracted.txt").read_text(encoding="utf-8")
                except OSError:
                    logger.warning("Chunk vector retrieval skipped unreadable source for %s", doc["uri"])
                    sources[doc["id"]] = None
                else:
                    sources[doc["id"]] = source, hashlib.sha256(source.encode()).hexdigest()
            if sources[doc["id"]] is None:
                continue
            text, source_hash = sources[doc["id"]]
            if source_hash != row["source_sha256"]:
                logger.warning("Original source changed for %s; rejecting stale chunk vectors", doc["uri"])
                continue
            start, end = row["start_offset"], row["end_offset"]
            results.append(LongDocRetrievalResult(
                context_id=doc["id"], uri=doc["uri"], strategy="vector",
                section_id=row["section_id"], snippet=text[start:end], snippet_offset=(start, end),
                relevance_score=float(row["similarity"])))
        return results
