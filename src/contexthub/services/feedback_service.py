"""FeedbackService: explicit feedback capture and quality scoring."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.feedback import (
    ContextFeedback,
    FeedbackOutcome,
    QualityReport,
    QualityReportItem,
)
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService

if TYPE_CHECKING:
    from contexthub.services.audit_service import AuditService


QUALITY_MIN_SAMPLES = 5


def _feedback_lock_key(
    account_id: str,
    context_id: UUID,
    retrieval_id: str,
    actor: str,
) -> int:
    """Deterministic 64-bit advisory lock key for one feedback idempotency key."""
    digest = hashlib.sha256(
        f"{account_id}\x00{context_id}\x00{retrieval_id}\x00{actor}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class FeedbackService:
    def __init__(self, acl: ACLService, audit: "AuditService | None" = None):
        self._acl = acl
        self._audit = audit

    async def record_feedback(
        self,
        db: ScopedRepo,
        context_uri: str,
        retrieval_id: str | None,
        outcome: str | FeedbackOutcome,
        ctx: RequestContext,
        metadata: dict | None = None,
    ) -> ContextFeedback:
        # Lock the context row up front so a concurrent delete cannot slip in
        # between existence/ACL checks and feedback/count writes.
        context_row = await db.fetchrow(
            """
            SELECT id
            FROM contexts
            WHERE uri = $1 AND status != 'deleted'
            FOR UPDATE
            """,
            context_uri,
        )
        if context_row is None:
            raise NotFoundError(f"Context {context_uri} not found")

        decision = await self._acl.check_read_access(db, context_uri, ctx)
        if not decision.allowed:
            raise ForbiddenError()

        normalized_outcome = self._normalize_outcome(outcome)
        final_retrieval_id = (retrieval_id or "").strip()
        degraded = not final_retrieval_id
        if degraded:
            final_retrieval_id = str(uuid4())

        context_id = context_row["id"]
        await db.fetchval(
            "SELECT pg_advisory_xact_lock($1)",
            _feedback_lock_key(
                ctx.account_id,
                context_id,
                final_retrieval_id,
                ctx.agent_id,
            ),
        )

        existing = await db.fetchrow(
            """
            SELECT id, outcome
            FROM context_feedback
            WHERE context_id = $1
              AND retrieval_id = $2
              AND actor = $3
              AND account_id = current_setting('app.account_id')
            """,
            context_id,
            final_retrieval_id,
            ctx.agent_id,
        )

        if existing is None:
            old_outcome = None
            row = await db.fetchrow(
                """
                INSERT INTO context_feedback (
                    context_id, retrieval_id, actor, retrieved_at,
                    outcome, metadata, account_id
                )
                VALUES (
                    $1, $2, $3, NOW(),
                    $4, $5::jsonb, current_setting('app.account_id')
                )
                RETURNING *
                """,
                context_id,
                final_retrieval_id,
                ctx.agent_id,
                normalized_outcome,
                metadata,
            )
        else:
            old_outcome = existing["outcome"]
            row = await db.fetchrow(
                """
                UPDATE context_feedback
                SET outcome = $2, metadata = $3::jsonb
                WHERE id = $1
                RETURNING *
                """,
                existing["id"],
                normalized_outcome,
                metadata,
            )

        await self._update_feedback_counts(
            db,
            context_id=context_id,
            new_outcome=normalized_outcome,
            old_outcome=old_outcome,
        )

        if self._audit:
            await self._audit.log_best_effort(
                db,
                ctx.agent_id,
                "feedback",
                context_uri,
                "success",
                metadata={
                    "retrieval_id": final_retrieval_id,
                    "outcome": normalized_outcome,
                    "degraded": degraded,
                },
            )

        return self._row_to_feedback(row)

    async def _update_feedback_counts(
        self,
        db: ScopedRepo,
        context_id: UUID,
        new_outcome: str,
        old_outcome: str | None,
    ) -> None:
        new_bucket = self._outcome_bucket(new_outcome)
        old_bucket = self._outcome_bucket(old_outcome)

        if old_bucket is None:
            if new_bucket == "adopted":
                await db.execute(
                    """
                    UPDATE contexts
                    SET adopted_count = adopted_count + 1
                    WHERE id = $1
                    """,
                    context_id,
                )
            else:
                await db.execute(
                    """
                    UPDATE contexts
                    SET ignored_count = ignored_count + 1
                    WHERE id = $1
                    """,
                    context_id,
                )
            return

        if old_bucket == new_bucket:
            return

        if old_bucket == "adopted":
            await db.execute(
                """
                UPDATE contexts
                SET adopted_count = GREATEST(adopted_count - 1, 0)
                WHERE id = $1
                """,
                context_id,
            )
        else:
            await db.execute(
                """
                UPDATE contexts
                SET ignored_count = GREATEST(ignored_count - 1, 0)
                WHERE id = $1
                """,
                context_id,
            )

        if new_bucket == "adopted":
            await db.execute(
                """
                UPDATE contexts
                SET adopted_count = adopted_count + 1
                WHERE id = $1
                """,
                context_id,
            )
        else:
            await db.execute(
                """
                UPDATE contexts
                SET ignored_count = ignored_count + 1
                WHERE id = $1
                """,
                context_id,
            )

    async def get_quality_score(
        self,
        db: ScopedRepo,
        context_id: UUID,
    ) -> float:
        row = await db.fetchrow(
            """
            SELECT adopted_count, ignored_count
            FROM contexts
            WHERE id = $1 AND status != 'deleted'
            """,
            context_id,
        )
        if row is None:
            raise NotFoundError(f"Context {context_id} not found")

        adopted_count = row["adopted_count"]
        ignored_count = row["ignored_count"]
        return adopted_count / (adopted_count + ignored_count + 1)

    async def generate_quality_report(
        self,
        db: ScopedRepo,
        min_active_count: int = 10,
        max_adoption_rate: float = 0.2,
        limit: int = 50,
    ) -> QualityReport:
        rows = await db.fetch(
            """
            SELECT
                context_id,
                uri,
                context_type,
                scope,
                active_count,
                adopted_count,
                ignored_count,
                adoption_rate,
                adopted_count::float / (adopted_count + ignored_count + 1)
                    AS quality_score
            FROM (
                SELECT
                    id AS context_id,
                    uri,
                    context_type,
                    scope,
                    active_count,
                    adopted_count,
                    ignored_count,
                    CASE
                        WHEN adopted_count + ignored_count = 0 THEN 0.0
                        ELSE adopted_count::float / (adopted_count + ignored_count)
                    END AS adoption_rate
                FROM contexts
                WHERE active_count > $1
                  AND adopted_count + ignored_count >= $2
                  AND status != 'deleted'
            ) AS c
            WHERE adoption_rate < $3
            ORDER BY active_count DESC, uri ASC
            LIMIT $4
            """,
            min_active_count,
            QUALITY_MIN_SAMPLES,
            max_adoption_rate,
            limit,
        )
        items = [QualityReportItem(**dict(row)) for row in rows]
        return QualityReport(
            items=items,
            total=len(items),
            min_active_count=min_active_count,
            max_adoption_rate=max_adoption_rate,
        )

    async def list_feedback(
        self,
        db: ScopedRepo,
        context_id: UUID | None = None,
        retrieval_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ContextFeedback]:
        conditions: list[str] = []
        args: list[object] = []

        if context_id is not None:
            conditions.append(f"context_id = ${len(args) + 1}")
            args.append(context_id)
        if retrieval_id is not None:
            conditions.append(f"retrieval_id = ${len(args) + 1}")
            args.append(retrieval_id)

        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        limit_idx = len(args) + 1
        offset_idx = len(args) + 2
        rows = await db.fetch(
            f"""
            SELECT *
            FROM context_feedback
            WHERE {where_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ${limit_idx}
            OFFSET ${offset_idx}
            """,
            *args,
            limit,
            offset,
        )
        return [self._row_to_feedback(row) for row in rows]

    def _normalize_outcome(self, outcome: str | FeedbackOutcome) -> str:
        normalized = str(outcome).strip().lower()
        try:
            return FeedbackOutcome(normalized).value
        except ValueError as exc:
            raise BadRequestError(f"Invalid feedback outcome: {outcome}") from exc

    def _row_to_feedback(self, row) -> ContextFeedback:
        return ContextFeedback(**dict(row))

    @staticmethod
    def _outcome_bucket(outcome: str | None) -> str | None:
        if outcome in ("adopted", "corrected"):
            return "adopted"
        if outcome in ("ignored", "irrelevant"):
            return "ignored"
        return None
