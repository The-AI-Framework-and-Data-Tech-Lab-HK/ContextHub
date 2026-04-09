"""RetrievalService: the single search owner for ContextHub."""

from __future__ import annotations

import logging

from contexthub.db.repository import ScopedRepo
from contexthub.llm.base import EmbeddingClient
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest, SearchResponse, SearchResult
from contexthub.retrieval.keyword_strategy import keyword_search
from contexthub.retrieval.router import RetrievalRouter
from contexthub.retrieval.vector_strategy import vector_search
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.masking_service import MaskingService

logger = logging.getLogger(__name__)

_STALE_PENALTY = 0.85


class RetrievalService:
    def __init__(
        self,
        retrieval_router: RetrievalRouter,
        embedding_client: EmbeddingClient,
        acl_service: ACLService,
        *,
        masking_service: MaskingService,
        audit_service: AuditService | None = None,
        over_retrieve_factor: int = 3,
    ):
        self._router = retrieval_router
        self._embedding = embedding_client
        self._acl = acl_service
        self._masking = masking_service
        self._audit = audit_service
        self._over_retrieve_factor = over_retrieve_factor

    async def search(
        self, db: ScopedRepo, request: SearchRequest, ctx: RequestContext
    ) -> SearchResponse:
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

        # 4. Stale penalty
        for c in candidates:
            if c.get("status") == "stale":
                score_key = "_rerank_score" if "_rerank_score" in c else "cosine_similarity"
                c[score_key] = c.get(score_key, 0) * _STALE_PENALTY

        # Re-sort after penalty
        score_key = "_rerank_score" if candidates and "_rerank_score" in candidates[0] else "cosine_similarity"
        candidates.sort(key=lambda x: x.get(score_key, 0), reverse=True)

        # 5. ACL filter (Phase 2: ACL-aware with field masks)
        acl_results = await self._acl.filter_visible_with_acl(db, candidates, ctx)
        candidates = [c for c, _ in acl_results]
        candidate_masks = [masks for _, masks in acl_results]

        # 6. Truncate to top_k
        candidates = candidates[: request.top_k]
        candidate_masks = candidate_masks[: request.top_k]

        # 7. L2 on demand
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

        # 8. Update active_count
        if candidates:
            ids = [c["id"] for c in candidates]
            await db.execute(
                "UPDATE contexts SET active_count = active_count + 1, last_accessed_at = NOW() WHERE id = ANY($1)",
                ids,
            )

        # 9. Build response (with masking)
        results = []
        for c, masks in zip(candidates, candidate_masks):
            final_score = c.get("_rerank_score", c.get("cosine_similarity", 0))

            l0 = c.get("l0_content")
            l1 = c.get("l1_content")
            l2 = c.get("l2_content")

            if masks:
                l0 = self._masking.apply_masks(l0, masks)
                l1 = self._masking.apply_masks(l1, masks)
                l2 = self._masking.apply_masks(l2, masks)

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
            ))

        response = SearchResponse(results=results, total=len(results))

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "search", None, "success",
                metadata={"query": request.query, "result_count": len(results)},
            )
        return response
