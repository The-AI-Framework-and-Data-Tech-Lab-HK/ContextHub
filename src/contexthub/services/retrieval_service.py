"""RetrievalService: the single search owner for ContextHub."""

from __future__ import annotations

import logging
from uuid import uuid4

from contexthub.db.repository import ScopedRepo
from contexthub.llm.base import EmbeddingClient
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest, SearchResponse, SearchResult
from contexthub.retrieval.keyword_strategy import keyword_search
from contexthub.retrieval.long_doc import LongDocRetrievalCoordinator
from contexthub.retrieval.router import RetrievalRouter
from contexthub.retrieval.vector_strategy import vector_search
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.feedback_service import QUALITY_MIN_SAMPLES
from contexthub.services.masking_service import MaskingService

logger = logging.getLogger(__name__)

_STALE_PENALTY = 0.85


def _quality_factor(adopted: int, ignored: int) -> float:
    total = adopted + ignored
    if total < QUALITY_MIN_SAMPLES:
        return 1.0
    quality_score = adopted / (total + 1)
    return 0.5 + 0.5 * quality_score


def _score_key(candidate: dict) -> str:
    return "_rerank_score" if "_rerank_score" in candidate else "cosine_similarity"


class RetrievalService:
    def __init__(
        self,
        retrieval_router: RetrievalRouter,
        embedding_client: EmbeddingClient,
        acl_service: ACLService,
        *,
        masking_service: MaskingService,
        audit_service: AuditService | None = None,
        long_doc_coordinator: LongDocRetrievalCoordinator | None = None,
        over_retrieve_factor: int = 3,
    ):
        self._router = retrieval_router
        self._embedding = embedding_client
        self._acl = acl_service
        self._masking = masking_service
        self._audit = audit_service
        self._long_doc_coordinator = long_doc_coordinator
        self._over_retrieve_factor = over_retrieve_factor

    async def search(
        self, db: ScopedRepo, request: SearchRequest, ctx: RequestContext
    ) -> SearchResponse:
        retrieval_id = str(uuid4())
        retrieve_k = request.top_k * self._over_retrieve_factor

        # 1. Embed query
        query_embedding = await self._embedding.embed(request.query)

        # 2. Retrieve candidates
        filter_types = [t.value for t in request.context_type] if request.context_type else None
        filter_scopes = [s.value for s in request.scope] if request.scope else None

        if query_embedding is not None:
            candidates = await vector_search(
                db, query_embedding, retrieve_k,
                context_types=filter_types,
                scopes=filter_scopes,
                include_stale=request.include_stale,
            )
        else:
            candidates = await keyword_search(
                db, request.query, retrieve_k,
                context_types=filter_types,
                scopes=filter_scopes,
                include_stale=request.include_stale,
            )

        # 3. Rerank
        candidates = await self._router.rerank.rerank(request.query, candidates)

        if self._long_doc_coordinator:
            candidates = await self._long_doc_coordinator.retrieve(
                db,
                request.query,
                candidates,
            )

        # 4. Quality factor
        if candidates:
            quality_rows = await db.fetch(
                """
                SELECT id, adopted_count, ignored_count
                FROM contexts
                WHERE id = ANY($1)
                """,
                [c["id"] for c in candidates],
            )
            quality_map = {
                row["id"]: (row["adopted_count"], row["ignored_count"])
                for row in quality_rows
            }
            for c in candidates:
                adopted_count, ignored_count = quality_map.get(c["id"], (0, 0))
                c["adopted_count"] = adopted_count
                c["ignored_count"] = ignored_count
                score_key = _score_key(c)
                c[score_key] = c.get(score_key, 0) * _quality_factor(
                    adopted_count, ignored_count
                )

        # 5. Stale penalty
        for c in candidates:
            if c.get("status") == "stale":
                score_key = _score_key(c)
                c[score_key] = c.get(score_key, 0) * _STALE_PENALTY

        # Re-sort after post-rerank score adjustments
        candidates.sort(key=lambda x: x.get(_score_key(x), 0), reverse=True)

        # 6. ACL filter (Phase 2: ACL-aware with field masks)
        acl_results = await self._acl.filter_visible_with_acl(db, candidates, ctx)
        candidates = [c for c, _ in acl_results]
        candidate_masks = [masks for _, masks in acl_results]

        # 7. Truncate to top_k
        candidates = candidates[: request.top_k]
        candidate_masks = candidate_masks[: request.top_k]

        # 8. L2 on demand
        if request.level.value == "L2" and candidates:
            ids = [c["id"] for c in candidates]
            placeholders = ", ".join(f"${i+1}" for i in range(len(ids)))
            l2_rows = await db.fetch(
                f"SELECT id, l2_content FROM contexts WHERE id IN ({placeholders})",
                *ids,
            )
            l2_map = {r["id"]: r["l2_content"] for r in l2_rows}
            for c in candidates:
                c["l2_content"] = l2_map.get(c["id"])

        # 9. Update active_count
        if candidates:
            ids = [c["id"] for c in candidates]
            await db.execute(
                "UPDATE contexts SET active_count = active_count + 1, last_accessed_at = NOW() WHERE id = ANY($1)",
                ids,
            )

        # 10. Build response (with masking)
        results = []
        for c, masks in zip(candidates, candidate_masks):
            final_score = c.get("_rerank_score", c.get("cosine_similarity", 0))

            l0 = c.get("l0_content")
            l1 = c.get("l1_content")
            l2 = c.get("l2_content")
            snippet = c.get("snippet")

            if masks:
                l0 = self._masking.apply_masks(l0, masks)
                l1 = self._masking.apply_masks(l1, masks)
                l2 = self._masking.apply_masks(l2, masks)
                if snippet is not None:
                    snippet = self._masking.apply_masks(snippet, masks)

            results.append(SearchResult(
                uri=c["uri"],
                context_type=c["context_type"],
                scope=c["scope"],
                owner_space=c.get("owner_space"),
                score=final_score,
                l0_content=l0,
                l1_content=l1,
                l2_content=l2,
                status=c["status"],
                version=c["version"],
                tags=c.get("tags", []),
                snippet=snippet,
                section_id=c.get("section_id"),
                retrieval_strategy=c.get("retrieval_strategy"),
            ))

        response = SearchResponse(
            results=results,
            total=len(results),
            retrieval_id=retrieval_id,
        )

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "search", None, "success",
                metadata={
                    "query": request.query,
                    "result_count": len(results),
                    "retrieval_id": retrieval_id,
                },
            )
        return response
