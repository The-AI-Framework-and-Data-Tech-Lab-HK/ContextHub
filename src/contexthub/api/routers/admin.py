"""Admin API: ACL policy CRUD, audit log query, share grant management."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from contexthub.api.deps import (
    get_acl_service,
    get_audit_service,
    get_db,
    get_feedback_service,
    get_lifecycle_scheduler,
    get_lifecycle_service,
    get_request_context,
    get_share_service,
)
from contexthub.db.repository import ScopedRepo
from contexthub.errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from contexthub.models.access import (
    AccessPolicy,
    CreatePolicyRequest,
    PolicyAction,
    UpdatePolicyRequest,
)
from contexthub.models.audit import AuditAction, AuditEntry, AuditResult
from contexthub.models.feedback import QualityReport
from contexthub.models.lifecycle import (
    CreateLifecyclePolicyRequest,
    LifecyclePolicy,
    LifecycleTransitionRequest,
)
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.feedback_service import FeedbackService
from contexthub.services.lifecycle_scheduler import LifecycleScheduler
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.share_service import ShareService

router = APIRouter(prefix="/api/v1", tags=["admin"])


# ── Permission check ────────────────────────────────────────────────────


async def _require_admin(db: ScopedRepo, ctx: RequestContext) -> None:
    """Verify the caller holds an admin role in at least one team of the current tenant.

    Must JOIN teams to leverage RLS (team_memberships has no RLS of its own).
    """
    has_admin = await db.fetchval(
        """
        SELECT 1 FROM team_memberships tm
        JOIN teams t ON t.id = tm.team_id
        WHERE tm.agent_id = $1 AND tm.role = 'admin'
        LIMIT 1
        """,
        ctx.agent_id,
    )
    if not has_admin:
        raise ForbiddenError("Admin role required")


@router.get("/admin/quality-report")
async def get_quality_report(
    min_active_count: int = Query(10, ge=0),
    max_adoption_rate: float = Query(0.2, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    feedback_svc: FeedbackService = Depends(get_feedback_service),
) -> QualityReport:
    await _require_admin(db, ctx)
    return await feedback_svc.generate_quality_report(
        db,
        min_active_count=min_active_count,
        max_adoption_rate=max_adoption_rate,
        limit=limit,
    )


@router.get("/admin/lifecycle/policies")
async def list_lifecycle_policies(
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    lifecycle_svc: LifecycleService = Depends(get_lifecycle_service),
) -> list[LifecyclePolicy]:
    await _require_admin(db, ctx)
    await lifecycle_svc.ensure_default_policies(db, ctx)
    rows = await db.fetch(
        """
        SELECT context_type, scope, stale_after_days, archive_after_days,
               delete_after_days, account_id, updated_at
        FROM lifecycle_policies
        ORDER BY context_type ASC, scope ASC
        """
    )
    return [LifecyclePolicy(**dict(row)) for row in rows]


@router.put("/admin/lifecycle/policies")
async def upsert_lifecycle_policy(
    body: CreateLifecyclePolicyRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    lifecycle_svc: LifecycleService = Depends(get_lifecycle_service),
) -> LifecyclePolicy:
    await _require_admin(db, ctx)
    return await lifecycle_svc.upsert_policy(
        db,
        context_type=body.context_type,
        scope=body.scope,
        stale_after_days=body.stale_after_days,
        archive_after_days=body.archive_after_days,
        delete_after_days=body.delete_after_days,
        ctx=ctx,
    )


@router.post("/admin/lifecycle/transition")
async def transition_lifecycle(
    body: LifecycleTransitionRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    lifecycle_svc: LifecycleService = Depends(get_lifecycle_service),
):
    await _require_admin(db, ctx)
    row = await db.fetchrow(
        """
        SELECT id, status
        FROM contexts
        WHERE uri = $1 AND status != 'deleted'
        """,
        body.context_uri,
    )
    if row is None:
        raise NotFoundError(f"Context {body.context_uri} not found")

    transition_map = {
        ("active", "stale"): lambda: lifecycle_svc.mark_stale(
            db,
            row["id"],
            body.reason or "manual_transition",
            ctx,
        ),
        ("stale", "active"): lambda: lifecycle_svc.recover_from_stale(db, row["id"], ctx),
        ("stale", "archived"): lambda: lifecycle_svc.mark_archived(db, row["id"], ctx),
        ("archived", "active"): lambda: lifecycle_svc.recover_from_archived(db, row["id"], ctx),
        ("archived", "deleted"): lambda: lifecycle_svc.mark_deleted(db, row["id"], ctx),
    }
    transition = transition_map.get((row["status"], body.target_status.value))
    if transition is None:
        raise BadRequestError(
            "Invalid lifecycle transition; lifecycle changes must follow the dedicated "
            "state graph instead of generic PATCH updates"
        )

    await transition()
    return {
        "ok": True,
        "context_uri": body.context_uri,
        "target_status": body.target_status.value,
    }


@router.post("/admin/lifecycle/sweep")
async def run_lifecycle_sweep(
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    scheduler: LifecycleScheduler | None = Depends(get_lifecycle_scheduler),
):
    await _require_admin(db, ctx)
    if scheduler is None:
        raise ServiceUnavailableError("Lifecycle scheduler is not configured")
    await scheduler.run_once()
    return {"ok": True}


# ── Policy CRUD ─────────────────────────────────────────────────────────


@router.post("/admin/policies", status_code=201)
async def create_policy(
    body: CreatePolicyRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
):
    await _require_admin(db, ctx)

    row = await db.fetchrow(
        """
        INSERT INTO access_policies
            (resource_uri_pattern, principal, effect, actions,
             conditions, field_masks, priority, account_id, created_by)
        VALUES ($1, $2, $3, $4::text[],
                $5::jsonb, $6, $7,
                current_setting('app.account_id'), $8)
        RETURNING id, resource_uri_pattern, principal, effect, actions,
                  conditions, field_masks, priority, account_id,
                  created_at, updated_at, created_by
        """,
        body.resource_uri_pattern,
        body.principal,
        body.effect.value,
        [a.value for a in body.actions],
        body.conditions,
        body.field_masks,
        body.priority,
        ctx.agent_id,
    )

    policy = AccessPolicy(**dict(row))

    await audit.log_strict(
        db, ctx.agent_id, "policy_change", body.resource_uri_pattern, "success",
        metadata={
            "operation": "create_policy",
            "policy_id": str(policy.id),
            "effect": body.effect.value,
            "actions": [a.value for a in body.actions],
        },
    )

    return policy.model_dump(mode="json")


@router.get("/admin/policies")
async def list_policies(
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    principal: str | None = Query(None),
    resource_uri_pattern: str | None = Query(None),
    effect: str | None = Query(None),
):
    await _require_admin(db, ctx)

    conditions = []
    args = []
    idx = 1

    if principal is not None:
        conditions.append(f"principal = ${idx}")
        args.append(principal)
        idx += 1
    if resource_uri_pattern is not None:
        conditions.append(f"resource_uri_pattern = ${idx}")
        args.append(resource_uri_pattern)
        idx += 1
    if effect is not None:
        conditions.append(f"effect = ${idx}")
        args.append(effect)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    rows = await db.fetch(
        f"""
        SELECT id, resource_uri_pattern, principal, effect, actions,
               conditions, field_masks, priority, account_id,
               created_at, updated_at, created_by
        FROM access_policies
        WHERE {where}
        ORDER BY created_at DESC
        """,
        *args,
    )
    return [AccessPolicy(**dict(r)).model_dump(mode="json") for r in rows]


@router.get("/admin/policies/{policy_id}")
async def get_policy(
    policy_id: UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
):
    await _require_admin(db, ctx)

    row = await db.fetchrow(
        """
        SELECT id, resource_uri_pattern, principal, effect, actions,
               conditions, field_masks, priority, account_id,
               created_at, updated_at, created_by
        FROM access_policies WHERE id = $1
        """,
        policy_id,
    )
    if row is None:
        raise NotFoundError(f"Policy {policy_id} not found")
    return AccessPolicy(**dict(row)).model_dump(mode="json")


@router.patch("/admin/policies/{policy_id}")
async def update_policy(
    policy_id: UUID,
    body: UpdatePolicyRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
):
    await _require_admin(db, ctx)

    existing = await db.fetchrow(
        "SELECT id FROM access_policies WHERE id = $1", policy_id,
    )
    if existing is None:
        raise NotFoundError(f"Policy {policy_id} not found")

    sets: list[str] = []
    args: list = []
    idx = 1

    if body.resource_uri_pattern is not None:
        sets.append(f"resource_uri_pattern = ${idx}")
        args.append(body.resource_uri_pattern)
        idx += 1
    if body.principal is not None:
        sets.append(f"principal = ${idx}")
        args.append(body.principal)
        idx += 1
    if body.effect is not None:
        sets.append(f"effect = ${idx}")
        args.append(body.effect.value)
        idx += 1
    if body.actions is not None:
        sets.append(f"actions = ${idx}::text[]")
        args.append([a.value for a in body.actions])
        idx += 1
    if body.conditions is not None:
        sets.append(f"conditions = ${idx}::jsonb")
        args.append(body.conditions)
        idx += 1
    if body.field_masks is not None:
        sets.append(f"field_masks = ${idx}")
        args.append(body.field_masks)
        idx += 1
    if body.priority is not None:
        sets.append(f"priority = ${idx}")
        args.append(body.priority)
        idx += 1

    if not sets:
        raise BadRequestError("No fields to update")

    sets.append("updated_at = NOW()")
    set_clause = ", ".join(sets)

    args.append(policy_id)
    id_idx = idx

    row = await db.fetchrow(
        f"""
        UPDATE access_policies SET {set_clause}
        WHERE id = ${id_idx}
        RETURNING id, resource_uri_pattern, principal, effect, actions,
                  conditions, field_masks, priority, account_id,
                  created_at, updated_at, created_by
        """,
        *args,
    )

    policy = AccessPolicy(**dict(row))

    await audit.log_strict(
        db, ctx.agent_id, "policy_change", policy.resource_uri_pattern, "success",
        metadata={
            "operation": "update_policy",
            "policy_id": str(policy_id),
            "changed_fields": [f for f in ("resource_uri_pattern", "principal", "effect",
                                           "actions", "conditions", "field_masks", "priority")
                               if getattr(body, f, None) is not None],
        },
    )

    return policy.model_dump(mode="json")


@router.delete("/admin/policies/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
):
    await _require_admin(db, ctx)

    row = await db.fetchrow(
        """
        SELECT id, resource_uri_pattern, principal, effect
        FROM access_policies WHERE id = $1
        """,
        policy_id,
    )
    if row is None:
        raise NotFoundError(f"Policy {policy_id} not found")

    await db.execute("DELETE FROM access_policies WHERE id = $1", policy_id)

    await audit.log_strict(
        db, ctx.agent_id, "policy_change", row["resource_uri_pattern"], "success",
        metadata={
            "operation": "delete_policy",
            "policy_id": str(policy_id),
            "effect": row["effect"],
        },
    )


# ── Audit query ─────────────────────────────────────────────────────────


@router.get("/admin/audit")
async def query_audit(
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    actor: str | None = Query(None),
    action: AuditAction | None = Query(None),
    resource_uri: str | None = Query(None),
    result: AuditResult | None = Query(None),
    start_time: datetime | None = Query(None),
    end_time: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    await _require_admin(db, ctx)

    conditions = []
    args = []
    idx = 1

    if actor is not None:
        conditions.append(f"actor = ${idx}")
        args.append(actor)
        idx += 1
    if action is not None:
        conditions.append(f"action = ${idx}")
        args.append(action.value if hasattr(action, "value") else action)
        idx += 1
    if resource_uri is not None:
        conditions.append(f"resource_uri = ${idx}")
        args.append(resource_uri)
        idx += 1
    if result is not None:
        conditions.append(f"result = ${idx}")
        args.append(result.value if hasattr(result, "value") else result)
        idx += 1
    if start_time is not None:
        conditions.append(f"timestamp >= ${idx}")
        args.append(start_time)
        idx += 1
    if end_time is not None:
        conditions.append(f"timestamp <= ${idx}")
        args.append(end_time)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    args.append(limit)
    limit_idx = idx
    idx += 1
    args.append(offset)
    offset_idx = idx

    rows = await db.fetch(
        f"""
        SELECT id, timestamp, actor, action, resource_uri,
               context_used, result, metadata, account_id,
               ip_address, request_id
        FROM audit_log
        WHERE {where}
        ORDER BY timestamp DESC
        LIMIT ${limit_idx} OFFSET ${offset_idx}
        """,
        *args,
    )
    return [AuditEntry(**dict(r)).model_dump(mode="json") for r in rows]


# ── Share grant management ──────────────────────────────────────────────


class ShareGrantRequest(BaseModel):
    source_uri: str
    target_principal: str
    field_masks: list[str] | None = None


@router.post("/shares", status_code=201)
async def create_share_grant(
    body: ShareGrantRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    share_svc: ShareService = Depends(get_share_service),
):
    policy = await share_svc.grant(
        db, body.source_uri, body.target_principal, ctx,
        field_masks=body.field_masks,
    )
    return policy.model_dump(mode="json")


@router.delete("/shares/{policy_id}", status_code=204)
async def revoke_share_grant(
    policy_id: UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    share_svc: ShareService = Depends(get_share_service),
):
    await share_svc.revoke(db, policy_id, ctx)


@router.get("/shares")
async def list_share_grants(
    source_uri: str = Query(...),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    share_svc: ShareService = Depends(get_share_service),
):
    grants = await share_svc.list_grants_by_source(db, source_uri, ctx)
    return [g.model_dump(mode="json") for g in grants]
