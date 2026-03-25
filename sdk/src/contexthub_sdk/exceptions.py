"""Structured exceptions for ContextHub SDK.

Each exception maps to a specific HTTP status code returned by the server.
"""

from __future__ import annotations


class ContextHubError(Exception):
    """Base exception for all ContextHub SDK errors."""

    status_code: int = 0

    def __init__(self, detail: str = "", *, status_code: int | None = None) -> None:
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail)


class BadRequestError(ContextHubError):
    status_code = 400


class AuthenticationError(ContextHubError):
    status_code = 401


class ForbiddenError(ContextHubError):
    status_code = 403


class NotFoundError(ContextHubError):
    status_code = 404


class ConflictError(ContextHubError):
    status_code = 409


class PreconditionRequiredError(ContextHubError):
    status_code = 428


class ServerError(ContextHubError):
    status_code = 500


_STATUS_MAP: dict[int, type[ContextHubError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    428: PreconditionRequiredError,
}


def raise_for_status(status_code: int, detail: str) -> None:
    """Raise the appropriate SDK exception for an HTTP error status code."""
    if status_code < 400:
        return
    exc_cls = _STATUS_MAP.get(status_code)
    if exc_cls is not None:
        raise exc_cls(detail)
    if status_code >= 500:
        raise ServerError(detail, status_code=status_code)
    raise ContextHubError(detail, status_code=status_code)
