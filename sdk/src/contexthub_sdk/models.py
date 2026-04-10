"""SDK-owned Pydantic models mirroring ContextHub server response shapes.

These models are independent of the server codebase — they are derived from
the server's actual HTTP response contracts, not re-exported from it.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Enums (mirroring server enums) ──────────────────────────────────────


class ContextLevel(str, enum.Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class ContextType(str, enum.Enum):
    TABLE_SCHEMA = "table_schema"
    SKILL = "skill"
    MEMORY = "memory"
    RESOURCE = "resource"


class Scope(str, enum.Enum):
    DATALAKE = "datalake"
    TEAM = "team"
    AGENT = "agent"
    USER = "user"


class ContextStatus(str, enum.Enum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"
    DELETED = "deleted"
    PENDING_REVIEW = "pending_review"


class SkillVersionStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


# ── Context models ──────────────────────────────────────────────────────


class ContextRecord(BaseModel):
    """Full context record as returned by create/update endpoints."""

    id: UUID
    uri: str
    context_type: ContextType
    scope: Scope
    owner_space: str | None = None
    account_id: str
    l0_content: str | None = None
    l1_content: str | None = None
    l2_content: str | None = None
    file_path: str | None = None
    status: ContextStatus = ContextStatus.ACTIVE
    version: int = 1
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    stale_at: datetime | None = None
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    active_count: int = 0
    adopted_count: int = 0
    ignored_count: int = 0


class ContextReadResult(BaseModel):
    """Non-skill context read result."""

    uri: str
    level: ContextLevel
    content: str


class ResolvedSkillReadResult(BaseModel):
    """Skill context read result with version resolution."""

    uri: str
    version: int
    content: str
    status: SkillVersionStatus
    advisory: str | None = None


class ContextStat(BaseModel):
    """Context stat information."""

    id: UUID
    uri: str
    context_type: ContextType
    scope: Scope
    owner_space: str | None = None
    status: ContextStatus
    version: int
    tags: list[str] = Field(default_factory=list)
    active_count: int
    adopted_count: int
    ignored_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None


class DependencyRecord(BaseModel):
    """A dependency entry returned by /contexts/{uri}/deps."""

    dep_type: str
    pinned_version: int | None = None
    dependent_uri: str
    dependency_uri: str


# ── Search models ───────────────────────────────────────────────────────


class SearchResult(BaseModel):
    uri: str
    context_type: ContextType
    scope: Scope
    owner_space: str | None = None
    score: float
    l0_content: str | None = None
    l1_content: str | None = None
    l2_content: str | None = None
    status: ContextStatus
    version: int
    tags: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int


# ── Memory models ───────────────────────────────────────────────────────


class MemoryRecord(BaseModel):
    """Memory summary as returned by GET /api/v1/memories."""

    uri: str
    l0_content: str | None = None
    status: ContextStatus
    version: int
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Skill models ────────────────────────────────────────────────────────


class SkillVersionRecord(BaseModel):
    skill_id: UUID
    version: int
    content: str
    changelog: str | None = None
    is_breaking: bool = False
    status: SkillVersionStatus = SkillVersionStatus.DRAFT
    published_by: str | None = None
    published_at: datetime | None = None


class SkillSubscriptionRecord(BaseModel):
    id: int | None = None
    agent_id: str
    skill_id: UUID
    pinned_version: int | None = None
    account_id: str
    created_at: datetime | None = None


# ── Policy / ACL models (Phase 2) ───────────────────────────────────────


class PolicyEffect(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyAction(str, enum.Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class AccessPolicyRecord(BaseModel):
    """ACL policy as returned by Admin API."""

    id: UUID
    resource_uri_pattern: str
    principal: str
    effect: PolicyEffect
    actions: list[PolicyAction]
    conditions: dict | None = None
    field_masks: list[str] | None = None
    priority: int = 0
    account_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str | None = None


# ── Audit models (Phase 2) ──────────────────────────────────────────────


class AuditAction(str, enum.Enum):
    WRITE = "write"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    PROMOTE = "promote"
    PUBLISH = "publish"
    ACCESS_DENIED = "access_denied"
    POLICY_CHANGE = "policy_change"
    READ = "read"
    SEARCH = "search"
    LS = "ls"
    STAT = "stat"


class AuditResult(str, enum.Enum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditEntryRecord(BaseModel):
    """Audit log entry as returned by Admin API."""

    id: UUID
    timestamp: datetime | None = None
    actor: str
    action: AuditAction
    resource_uri: str | None = None
    context_used: list[str] | None = None
    result: AuditResult
    metadata: dict | None = None
    account_id: str
    ip_address: str | None = None
    request_id: UUID | None = None
