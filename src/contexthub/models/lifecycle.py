from datetime import datetime

from pydantic import BaseModel, Field

from contexthub.models.context import ContextStatus, ContextType, Scope


class LifecyclePolicy(BaseModel):
    """对应 lifecycle_policies 表的完整模型。"""

    context_type: str
    scope: str
    stale_after_days: int = Field(default=0, ge=0)
    archive_after_days: int = Field(default=0, ge=0)
    delete_after_days: int = Field(default=0, ge=0)
    account_id: str
    updated_at: datetime | None = None


class CreateLifecyclePolicyRequest(BaseModel):
    """创建生命周期策略的请求体。"""

    context_type: ContextType
    scope: Scope
    stale_after_days: int = Field(default=0, ge=0)
    archive_after_days: int = Field(default=0, ge=0)
    delete_after_days: int = Field(default=0, ge=0)


class UpdateLifecyclePolicyRequest(BaseModel):
    """更新生命周期策略的请求体。所有字段可选，只更新非 None 字段。"""

    stale_after_days: int | None = Field(default=None, ge=0)
    archive_after_days: int | None = Field(default=None, ge=0)
    delete_after_days: int | None = Field(default=None, ge=0)


class LifecycleTransitionRequest(BaseModel):
    """手动触发状态转换的请求体（Admin API）。"""

    context_uri: str
    target_status: ContextStatus
    reason: str | None = None
