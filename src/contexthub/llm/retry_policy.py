"""Explicit S2 retry policy. No default timeout, backoff, model or price."""

import asyncio
import math
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from pydantic import Field, model_validator

from contexthub.models.execution import CallIdentity, PriceBasis
from contexthub.models.knowledge import Contract, ContractError, Positive
from contexthub.services.execution_ledger import AttemptSink


class ResponseParseError(ValueError):
    pass


class ResponseSchemaError(ValueError):
    pass


class RetryPolicy(Contract):
    max_attempts: Positive = Field(le=3)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    backoff_seconds: tuple[float, ...]
    retry_status_codes: tuple[int, ...]
    retry_transport_errors: tuple[str, ...]

    @model_validator(mode="after")
    def validate_policy(self):
        if len(self.backoff_seconds) != self.max_attempts - 1 or any(
            not math.isfinite(v) or v < 0 for v in self.backoff_seconds
        ):
            raise ContractError("invalid_backoff")
        if any(c != 429 and not 500 <= c <= 599 for c in self.retry_status_codes):
            raise ContractError("non_transient_retry_status")
        allowed = {
            "ConnectError",
            "ReadError",
            "WriteError",
            "RemoteProtocolError",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
        }
        if not set(self.retry_transport_errors) <= allowed:
            raise ContractError("non_transient_retry_class")
        return self

    def is_retryable(self, error: BaseException) -> bool:
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in self.retry_status_codes
        return (
            isinstance(error, httpx.TransportError)
            and type(error).__name__ in self.retry_transport_errors
        )


@dataclass(frozen=True)
class RecordedCall:
    identity: CallIdentity
    policy: RetryPolicy
    recorder: AttemptSink
    price: PriceBasis | None = None
    # The validator runs inside the attempt so schema failures preserve usage.
    validate_response: Callable[[str], None] | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
