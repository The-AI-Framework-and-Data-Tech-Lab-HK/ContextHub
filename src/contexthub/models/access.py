from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyAction(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class AccessPolicy(BaseModel):
    """对应 access_policies 表的完整模型。"""
    id: UUID
    resource_uri_pattern: str
    principal: str
    effect: PolicyEffect
    actions: list[PolicyAction] = Field(min_length=1)
    conditions: dict | None = None
    field_masks: list[str] | None = None
    priority: int = 0
    account_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str | None = None


class CreatePolicyRequest(BaseModel):
    """创建 ACL 策略的请求体。"""
    resource_uri_pattern: str
    principal: str
    effect: PolicyEffect
    actions: list[PolicyAction] = Field(min_length=1)
    conditions: dict | None = None
    field_masks: list[str] | None = None
    priority: int = 0


class UpdatePolicyRequest(BaseModel):
    """更新 ACL 策略的请求体。所有字段可选，只更新非 None 字段。"""
    resource_uri_pattern: str | None = None
    principal: str | None = None
    effect: PolicyEffect | None = None
    actions: list[PolicyAction] | None = Field(default=None, min_length=1)
    conditions: dict | None = None
    field_masks: list[str] | None = None
    priority: int | None = None
