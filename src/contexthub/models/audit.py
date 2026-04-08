from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class AuditAction(StrEnum):
    # Tier 1: fail-closed
    WRITE = "write"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    PROMOTE = "promote"
    PUBLISH = "publish"
    ACCESS_DENIED = "access_denied"
    POLICY_CHANGE = "policy_change"
    # Tier 2: best-effort
    READ = "read"
    SEARCH = "search"
    LS = "ls"
    STAT = "stat"


class AuditResult(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditEntry(BaseModel):
    """对应 audit_log 表的完整模型。"""
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


class AuditQueryRequest(BaseModel):
    """审计日志查询请求（用于 Admin API 查询端点）。"""
    actor: str | None = None
    action: AuditAction | None = None
    resource_uri: str | None = None
    result: AuditResult | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
