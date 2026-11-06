"""Index exact source slices; no generation, summaries, or body duplication."""
from __future__ import annotations

import hashlib

from contexthub.errors import ServiceUnavailableError
from contexthub.llm.base import EmbeddingClient, NoOpEmbeddingClient
from contexthub.retrieval.long_doc.ranges import document_ranges, embedding_input, enclosing_section


class DocumentChunkIndexer:
    def __init__(self, embedding_client: EmbeddingClient):
        self._embedding = embedding_client

    async def index(self, db, document: dict, text: str, sections: list[dict]) -> int:
        if isinstance(self._embedding, NoOpEmbeddingClient):
            return 0
        ranges = document_ranges(text, sections)
        if not sections:
            raise ValueError("Persist document sections before indexing original ranges")
        inputs = [embedding_input(text, sections, start, end) for start, end in ranges]
        vectors = []
        # Reuse the existing provider's batch capability and ten-input batching.
        for left in range(0, len(inputs), 10):
            batch = inputs[left:left + 10]
            try:
                if hasattr(self._embedding, "embed_batch"):
                    result = await self._embedding.embed_batch(batch)
                else:
                    result = [await self._embedding.embed(value) for value in batch]
            except Exception as exc:
                raise ServiceUnavailableError("Document chunk embedding failed; no partial index was saved") from exc
            if len(result) != len(batch) or any(vector is None for vector in result):
                raise ServiceUnavailableError("Document chunk embedding failed; no partial index was saved")
            vectors.extend(result)
        source_hash = hashlib.sha256(text.encode()).hexdigest()
        # The caller's transaction owns these writes and the document revision.
        for (start, end), vector in zip(ranges, vectors):
            section = enclosing_section(sections, start, end)
            await db.execute("""INSERT INTO document_chunk_embeddings (
                account_id, context_id, context_version, section_id,
                start_offset, end_offset, source_sha256, embedding
            ) VALUES (current_setting('app.account_id'), $1, $2, $3, $4, $5, $6, $7::vector)""",
                document["id"], document["version"], section["section_id"], start, end, source_hash,
                "[" + ",".join(str(value) for value in vector) + "]")
        return len(ranges)
