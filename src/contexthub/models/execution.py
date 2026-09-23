"""Per-call identities and append-only attempt events, independent of counters."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from contexthub.models.knowledge import (
    Contract,
    ContractError,
    Hash,
    Name,
    Nonnegative,
    Positive,
    canonical_hash,
)


class CallIdentity(Contract):
    account_id: Name
    execution_id: UUID
    call_id: UUID
    event_id: UUID | None = None
    work_item_id: UUID | None = None
    strategy: Name
    stage: Name
    operation: Name
    model_role: Name
    code_version: Name
    config_sha256: Hash


class Usage(Contract):
    input_tokens: Nonnegative | None = None
    output_tokens: Nonnegative | None = None
    total_tokens: Nonnegative | None = None
    cached_input_tokens: Nonnegative | None = None
    reasoning_tokens: Nonnegative | None = None
    completeness: Literal["complete", "partial", "missing", "inconsistent"]
    missing_reason: Name | None = None

    @model_validator(mode="after")
    def consistent(self):
        known = self.input_tokens is not None and self.output_tokens is not None
        if (
            self.completeness != "inconsistent"
            and (self.completeness == "complete") != known
        ):
            raise ContractError("usage_completeness_mismatch")
        if self.completeness != "complete" and self.missing_reason is None:
            raise ContractError("usage_missing_reason_required")
        if self.completeness == "missing" and any(
            v is not None
            for v in (self.input_tokens, self.output_tokens, self.total_tokens)
        ):
            raise ContractError("usage_completeness_mismatch")
        if self.completeness == "complete" and (
            (
                self.total_tokens is not None
                and self.total_tokens != self.input_tokens + self.output_tokens
            )
            or (
                self.cached_input_tokens is not None
                and self.cached_input_tokens > self.input_tokens
            )
        ):
            raise ContractError("usage_inconsistent")
        return self


class PriceBasis(Contract):
    """Explicit simple token tariff. Other billing schemes remain unknown in S2."""

    source: Name
    observed_at: AwareDatetime
    currency: Name
    region: Name
    billing_tier: Name
    model: Name
    unit_tokens: Positive
    input_rate: Decimal = Field(ge=0, allow_inf_nan=False)
    output_rate: Decimal = Field(ge=0, allow_inf_nan=False)
    cached_input_rate: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class Cost(Contract):
    status: Literal["exact", "unknown"]
    amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = None
    price_sha256: Hash | None = None
    reason: Name | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "exact" and (
            self.amount is None
            or not self.currency
            or not self.price_sha256
            or self.reason
        ):
            raise ContractError("invalid_exact_cost")
        if self.status == "unknown" and (
            self.amount is not None or self.reason is None
        ):
            raise ContractError("unknown_cost_is_not_zero")
        return self


def calculate_cost(
    usage: Usage, price: PriceBasis | None, actual_model: str | None
) -> Cost:
    reason = None
    if price is None:
        reason = "price_missing"
    elif actual_model != price.model:
        reason = "price_model_mismatch"
    elif usage.completeness != "complete":
        reason = "usage_incomplete"
    elif usage.cached_input_tokens and price.cached_input_rate is None:
        reason = "cached_price_missing"
    elif (usage.cached_input_tokens or 0) > usage.input_tokens:
        reason = "usage_inconsistent"
    if reason:
        return Cost(
            status="unknown",
            reason=reason,
            currency=price.currency if price else None,
            price_sha256=canonical_hash(price) if price else None,
        )
    cached = usage.cached_input_tokens or 0
    amount = (
        (usage.input_tokens - cached) * price.input_rate
        + cached * (price.cached_input_rate or Decimal(0))
        + usage.output_tokens * price.output_rate
    ) / price.unit_tokens
    return Cost(
        status="exact",
        amount=amount,
        currency=price.currency,
        price_sha256=canonical_hash(price),
    )


class AttemptError(Contract):
    code: Name
    error_class: Name
    retryable: bool
    # Sanitized classifications, deliberately no exception message/body/headers.


class LocalWork(Contract):
    metric: Literal[
        "retrieval_pages",
        "retrieval_rows",
        "retrieval_bytes",
        "rule_checks",
        "hard_checks",
        "writes",
        "audit_records",
    ]
    count: Nonnegative


class AttemptRecord(Contract):
    identity: CallIdentity
    attempt_no: Positive = Field(le=3)
    phase: Literal["started", "finished"]
    requested_model: Name | None
    actual_model: Name | None = None
    actual_model_missing_reason: Name | None = None
    request_id: Name | None = None
    request_id_missing_reason: Name | None = None
    input_sha256: Hash
    prompt_sha256: Hash
    request_config_sha256: Hash
    retry_policy_sha256: Hash
    output_sha256: Hash | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    wall_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    outcome: Literal["started", "success", "error", "cancelled"]
    error: AttemptError | None = None
    stop_reason: (
        Literal[
            "success",
            "non_retryable",
            "retry_scheduled",
            "retry_exhausted",
            "cancelled",
        ]
        | None
    ) = None
    status_code: int | None = None
    usage: Usage
    price: PriceBasis | None = None
    cost: Cost
    local_work: tuple[LocalWork, ...] = ()

    @model_validator(mode="after")
    def consistency(self):
        if self.actual_model is None and self.actual_model_missing_reason is None:
            raise ContractError("model_missing_reason_required")
        if self.request_id is None and self.request_id_missing_reason is None:
            raise ContractError("request_id_missing_reason_required")
        if self.phase == "started":
            if (
                self.outcome != "started"
                or self.finished_at is not None
                or self.stop_reason is not None
                or self.error is not None
                or self.wall_seconds is not None
                or self.usage.completeness != "missing"
            ):
                raise ContractError("invalid_attempt_start")
        else:
            if (
                self.finished_at is None
                or self.wall_seconds is None
                or self.finished_at < self.started_at
                or self.stop_reason is None
            ):
                raise ContractError("invalid_attempt_finish")
            if (self.outcome in ("error", "cancelled")) != (
                self.error is not None
            ) or self.outcome == "started":
                raise ContractError("attempt_error_mismatch")
            if self.outcome == "success" and self.stop_reason != "success":
                raise ContractError("attempt_stop_mismatch")
            if self.outcome == "cancelled" and self.stop_reason != "cancelled":
                raise ContractError("attempt_stop_mismatch")
            if self.outcome == "error" and self.stop_reason not in (
                ("retry_scheduled", "retry_exhausted")
                if self.error.retryable
                else ("non_retryable",)
            ):
                raise ContractError("attempt_stop_mismatch")
            if self.attempt_no == 3 and self.stop_reason == "retry_scheduled":
                raise ContractError("retry_limit_exceeded")
        if self.cost != calculate_cost(self.usage, self.price, self.actual_model):
            raise ContractError("cost_mismatch")
        return self
