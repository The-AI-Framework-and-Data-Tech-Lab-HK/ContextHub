"""RetrievalService: the single search owner for ContextHub."""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4

from contexthub.db.repository import ScopedRepo
from contexthub.llm.base import EmbeddingClient
from contexthub.models.request import RequestContext
from contexthub.models.search import (
    SearchRequest,
    SearchResponse,
    SearchResult,
    StaleNotice,
)
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


def _notice_reason(validity_reason: str | None, source_uri: str | None) -> str | None:
    """The recorded reason, or a structural fallback naming the upstream.

    ``validity_reason`` is the propagation rule's own wording and is preferred.
    Closure descendants get the generic ``dependency closure invalidated by
    <uuid>`` text, which names a node by raw id and says nothing useful; when an
    upstream URI is known, name that instead.
    """
    if validity_reason and not validity_reason.startswith(
        "dependency closure invalidated by "
    ):
        return validity_reason
    if source_uri:
        return f"an upstream note it depends on changed: {source_uri}"
    return validity_reason


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

    async def _build_stale_notices(
        self, db: ScopedRepo, withheld: list[dict], ctx: RequestContext
    ) -> list[StaleNotice]:
        """Say what was withheld and why, instead of leaving a silent hole.

        Reads only facts the system already recorded: the withheld node's own
        body (the superseded prior value), ``contexts.validity_reason`` (written
        verbatim by ``LifecycleService.mark_stale``), and the nearest upstream
        node from its unresolved ``context_invalidations`` row.  Nothing is
        generated or inferred.

        The nearest upstream is deliberate: for a multi-hop closure the useful
        answer names the immediate predecessor that just went stale, not the
        original root.  ``source_context_id`` already holds that node for closure
        descendants; for the directly-marked node it points at itself, so the
        cause event's own node is used instead.
        """
        by_id: dict = {}
        for candidate in withheld:
            by_id.setdefault(candidate["id"], candidate)
        if not by_id:
            return []

        visible = {
            self._get_id(candidate): masks
            for candidate, masks in await self._acl.filter_visible_with_acl(
                db, list(by_id.values()), ctx
            )
        }
        if not visible:
            return []

        upstream_rows = await db.fetch(
            """
            SELECT DISTINCT ON (i.context_id)
                   i.context_id,
                   NULLIF(
                     COALESCE(
                       NULLIF(i.source_context_id, i.context_id),
                       e.context_id
                     ),
                     i.context_id
                   ) AS upstream_id
              FROM context_invalidations i
              JOIN change_events e ON e.event_id = i.cause_event_id
             WHERE i.context_id = ANY($1::uuid[])
               AND i.resolved_at IS NULL
             ORDER BY i.context_id, i.created_at DESC
            """,
            list(visible),
        )
        upstream_by_node = {
            row["context_id"]: row["upstream_id"]
            for row in upstream_rows
            if row["upstream_id"] is not None
        }

        # The upstream node is a separate read: it goes through the same ACL
        # filter, so an unreadable upstream is simply omitted from the notice.
        upstream_by_id: dict = {}
        upstream_masks: dict = {}
        upstream_ids = sorted(set(upstream_by_node.values()), key=str)
        if upstream_ids:
            rows = await db.fetch(
                """
                SELECT id, uri, context_type, scope, owner_space, status,
                       version, l0_content, l1_content
                  FROM contexts
                 WHERE id = ANY($1::uuid[])
                """,
                upstream_ids,
            )
            for row, masks in await self._acl.filter_visible_with_acl(
                db, [dict(row) for row in rows], ctx
            ):
                upstream_by_id[self._get_id(row)] = row
                upstream_masks[self._get_id(row)] = masks

        notices: list[StaleNotice] = []
        seen: set = set()
        for candidate in withheld:
            node_id = candidate["id"]
            if node_id in seen or node_id not in visible:
                continue
            seen.add(node_id)
            masks = visible[node_id]
            upstream = upstream_by_id.get(upstream_by_node.get(node_id))
            source_uri = upstream["uri"] if upstream is not None else None
            notices.append(
                StaleNotice(
                    uri=candidate["uri"],
                    validity_status=candidate.get("validity_status") or "unknown",
                    reason=_notice_reason(candidate.get("validity_reason"), source_uri),
                    stale_content=self._mask(
                        candidate.get("l1_content") or candidate.get("l0_content"),
                        masks,
                    ),
                    version=candidate.get("version"),
                    source_uri=source_uri,
                    source_content=self._mask(
                        (upstream.get("l1_content") or upstream.get("l0_content"))
                        if upstream is not None
                        else None,
                        upstream_masks.get(self._get_id(upstream))
                        if upstream is not None
                        else None,
                    ),
                )
            )
        return notices

    @staticmethod
    def _get_id(candidate):
        return candidate["id"] if isinstance(candidate, dict) else candidate.id

    def _mask(self, content: str | None, masks) -> str | None:
        return self._masking.apply_masks(content, masks) if masks else content

    async def _retrieve_non_fresh(
        self,
        db: ScopedRepo,
        request: SearchRequest,
        query_embedding,
        retrieve_k: int,
        *,
        context_types: list[str] | None,
        scopes: list[str] | None,
        already_seen: set,
    ) -> list[dict]:
        """Non-fresh matches for the stale-notice pass, excluding ones already read.

        Same query, same scoring, same bound as the main pass -- only the freshness
        filter is inverted.  Rows are candidates for *explanation* only; the caller
        never puts them back into the served results.
        """
        if query_embedding is not None:
            rows = await vector_search(
                db, query_embedding, retrieve_k,
                context_types=context_types,
                scopes=scopes,
                only_non_fresh=True,
            )
        else:
            rows = await keyword_search(
                db, request.query, retrieve_k,
                context_types=context_types,
                scopes=scopes,
                only_non_fresh=True,
            )
        return [row for row in rows if row["id"] not in already_seen]

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
            raw_candidates = await vector_search(
                db, query_embedding, retrieve_k,
                context_types=filter_types,
                scopes=filter_scopes,
                include_stale=True,
            )
        else:
            raw_candidates = await keyword_search(
                db, request.query, retrieve_k,
                context_types=filter_types,
                scopes=filter_scopes,
                include_stale=True,
            )
        candidate_trace: list[dict] = []
        candidates = []
        # Nodes held back because they are no longer valid.  Collected only to
        # explain the omission; they never rejoin ``candidates``.
        withheld: list[dict] = []
        want_notices = request.include_stale_notices and not request.include_stale
        if want_notices:
            # Both strategies order fresh rows first and then apply LIMIT, so a
            # stale row is dropped outright once the fresh matches alone fill
            # retrieve_k -- and then there is nothing left to explain, which made
            # notices silently depend on how many fresh rows happened to match.
            # This second pass asks only for the complement of the fresh set, so
            # it takes no slots from the served candidates and cannot change which
            # rows are servable. (Result *order* among equally-scored rows is not
            # stable in either case -- neither strategy has a deterministic
            # tiebreak -- so this is a claim about the served set, not its order.)
            raw_candidates = raw_candidates + await self._retrieve_non_fresh(
                db,
                request,
                query_embedding,
                retrieve_k,
                context_types=filter_types,
                scopes=filter_scopes,
                already_seen={candidate["id"] for candidate in raw_candidates},
            )
        for candidate in raw_candidates:
            reasons: list[str] = []
            if candidate.get("status") != "active":
                reasons.append(f"status:{candidate.get('status')}")
            validity = candidate.get("validity_status") or (
                "fresh" if candidate.get("status") == "active"
                else str(candidate.get("status") or "unknown")
            )
            if validity != "fresh":
                reasons.append(f"validity:{validity}")
            filtered = bool(reasons) and not request.include_stale
            candidate_trace.append(
                {
                    "node_id": str(candidate["id"]),
                    "version": candidate.get("version"),
                    "status": candidate.get("status"),
                    "validity_status": validity,
                    "filtered": filtered,
                    "filter_reasons": reasons if filtered else [],
                }
            )
            if not filtered:
                candidates.append(candidate)
            elif want_notices:
                withheld.append({**candidate, "validity_status": validity})

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

        # 7. MVCC read barrier.  Candidate search and reranking can race with a
        # concurrent invalidation, so validate the exact node/version/status
        # immediately before materialization.
        revalidated: list[dict] = []
        revalidated_masks: list[list] = []
        if candidates:
            rows = await db.fetch(
                """
                SELECT id, version, status, validity_status
                  FROM contexts
                 WHERE id = ANY($1::uuid[])
                """,
                [candidate["id"] for candidate in candidates],
            )
            current = {row["id"]: row for row in rows}
            for candidate, masks in zip(candidates, candidate_masks):
                row = current.get(candidate["id"])
                same_version = row is not None and row["version"] == candidate.get("version")
                serviceable = bool(
                    same_version
                    and row["status"] == "active"
                    and row["validity_status"] == "fresh"
                )
                if same_version and (serviceable or request.include_stale):
                    candidate["status"] = row["status"]
                    candidate["validity_status"] = row["validity_status"]
                    revalidated.append(candidate)
                    revalidated_masks.append(masks)
                    continue
                if same_version and want_notices:
                    # Invalidated between search and materialization: this is
                    # exactly the race that leaves an unexplained hole.
                    withheld.append(
                        {**candidate, "validity_status": row["validity_status"]}
                    )
                for trace_item in candidate_trace:
                    if trace_item["node_id"] == str(candidate["id"]):
                        trace_item["filtered"] = True
                        trace_item["filter_reasons"].append(
                            "materialization_version_changed"
                            if not same_version
                            else f"materialization_validity:{row['validity_status']}"
                        )
                        break
        candidates = revalidated
        candidate_masks = revalidated_masks

        # 7b. Explain the omissions.  Same ACL and masking path as servable
        # results, so a notice can never reveal a node the caller may not read.
        stale_notices = (
            await self._build_stale_notices(db, withheld, ctx) if withheld else []
        )

        # 8. Truncate to top_k
        candidates = candidates[: request.top_k]
        candidate_masks = candidate_masks[: request.top_k]

        # 9. L2 on demand; repeat the exact-version barrier on the body read.
        if request.level.value == "L2" and candidates:
            l2_rows = await db.fetch(
                """
                SELECT id, version, l2_content
                  FROM contexts
                 WHERE id = ANY($1::uuid[])
                   AND ($2 OR (status = 'active' AND validity_status = 'fresh'))
                """,
                [c["id"] for c in candidates],
                request.include_stale,
            )
            l2_map = {(r["id"], r["version"]): r["l2_content"] for r in l2_rows}
            kept = [
                (c, masks)
                for c, masks in zip(candidates, candidate_masks)
                if (c["id"], c.get("version")) in l2_map
            ]
            candidates = [item[0] for item in kept]
            candidate_masks = [item[1] for item in kept]
            for c in candidates:
                c["l2_content"] = l2_map[(c["id"], c.get("version"))]

        # 10. Update active_count
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
                validity_status=c.get("validity_status", "unknown"),
                tags=c.get("tags", []),
                snippet=snippet,
                section_id=c.get("section_id"),
                retrieval_strategy=c.get("retrieval_strategy"),
            ))

        final_versions = [
            {
                "node_id": str(candidate["id"]),
                "version": candidate.get("version"),
                "validity_status": candidate.get("validity_status", "unknown"),
            }
            for candidate in candidates
        ]
        context_hash = hashlib.sha256(
            json.dumps(
                [
                    {
                        **version,
                        "l0_hash": hashlib.sha256(
                            (candidate.get("l0_content") or "").encode("utf-8")
                        ).hexdigest(),
                        "l1_hash": hashlib.sha256(
                            (candidate.get("l1_content") or "").encode("utf-8")
                        ).hexdigest(),
                        "l2_hash": hashlib.sha256(
                            (candidate.get("l2_content") or "").encode("utf-8")
                        ).hexdigest(),
                        "service_content_hash": hashlib.sha256(
                            (
                                candidate.get("l1_content")
                                or candidate.get("l0_content")
                                or candidate.get("l2_content")
                                or ""
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                    for version, candidate in zip(final_versions, candidates)
                ],
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "query": request.query,
                    "scope": filter_scopes,
                    "context_type": filter_types,
                    "top_k": request.top_k,
                    "level": request.level.value,
                    "include_stale": request.include_stale,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        trace = {
            "candidates": candidate_trace,
            "final_versions": final_versions,
            "final_materialized": [
                {
                    "node_id": str(candidate["id"]),
                    "uri": candidate["uri"],
                    "version": candidate.get("version"),
                    "status": candidate.get("status"),
                    "validity_status": candidate.get("validity_status"),
                    "l0_content": candidate.get("l0_content"),
                    "l1_content": candidate.get("l1_content"),
                    "l2_content": candidate.get("l2_content"),
                    "service_content": (
                        candidate.get("l1_content")
                        or candidate.get("l0_content")
                        or candidate.get("l2_content")
                        or ""
                    ),
                }
                for candidate in candidates
            ],
            "context_hash": context_hash,
            "unsafe_debug_read": request.include_stale,
            "stale_notices": [notice.model_dump(mode="json") for notice in stale_notices],
        }
        await db.execute(
            """
            INSERT INTO retrieval_trace (
              retrieval_id, account_id, agent_id, request_hash,
              candidates, final_versions, context_hash
            )
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
            """,
            retrieval_id,
            ctx.account_id,
            ctx.agent_id,
            request_hash,
            json.dumps(candidate_trace, sort_keys=True),
            json.dumps(final_versions, sort_keys=True),
            context_hash,
        )
        response = SearchResponse(
            results=results,
            total=len(results),
            retrieval_id=retrieval_id,
            trace=trace,
            stale_notices=stale_notices,
        )

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "search", None, "success",
                metadata={
                    "query_hash": request_hash,
                    "result_count": len(results),
                    "retrieval_id": retrieval_id,
                    "context_hash": context_hash,
                },
            )
        return response
