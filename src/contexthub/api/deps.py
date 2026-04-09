"""FastAPI dependencies: RequestContext assembly and DB session."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, Request

from contexthub.db.repository import ScopedRepo
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.context_service import ContextService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.memory_service import MemoryService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.services.skill_service import SkillService
from contexthub.services.masking_service import MaskingService
from contexthub.store.context_store import ContextStore
from contexthub.services.catalog_sync_service import CatalogSyncService
from contexthub.services.share_service import ShareService


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


def get_acl_service(request: Request) -> ACLService:
    return request.app.state.acl_service


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


def get_skill_service(request: Request) -> SkillService:
    return request.app.state.skill_service


def get_retrieval_service(request: Request) -> RetrievalService:
    return request.app.state.retrieval_service


def get_indexer_service(request: Request) -> IndexerService:
    return request.app.state.indexer_service


def get_masking_service(request: Request) -> MaskingService:
    return request.app.state.masking_service


def get_catalog_sync_service(request: Request) -> CatalogSyncService:
    return request.app.state.catalog_sync_service


def get_audit_service(request: Request) -> AuditService:
    return request.app.state.audit_service


def get_share_service(request: Request) -> ShareService:
    return request.app.state.share_service
