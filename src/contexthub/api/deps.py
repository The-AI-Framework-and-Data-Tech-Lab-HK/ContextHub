"""FastAPI dependencies: RequestContext assembly and DB session."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, Request

from contexthub.db.repository import ScopedRepo
from contexthub.models.request import RequestContext
from contexthub.services.context_service import ContextService
from contexthub.store.context_store import ContextStore


async def get_request_context(
    x_account_id: str = Header(..., alias="X-Account-Id"),
    x_agent_id: str = Header(..., alias="X-Agent-Id"),
    if_match: int | None = Header(None, alias="If-Match"),
) -> RequestContext:
    return RequestContext(
        account_id=x_account_id,
        agent_id=x_agent_id,
        expected_version=if_match,
    )


async def get_db(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
) -> AsyncIterator[ScopedRepo]:
    async with request.app.state.repo.session(ctx.account_id) as db:
        yield db


def get_context_service(request: Request) -> ContextService:
    return request.app.state.context_service


def get_context_store(request: Request) -> ContextStore:
    return request.app.state.context_store
