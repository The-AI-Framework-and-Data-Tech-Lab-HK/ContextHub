"""Application entry point."""

from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from contexthub.api.middleware import AuthMiddleware
from contexthub.api.routers.contexts import router as contexts_router
from contexthub.api.routers.memories import router as memories_router
from contexthub.api.routers.feedback import router as feedback_router
from contexthub.api.routers.documents import router as documents_router
from contexthub.api.routers.search import router as search_router
from contexthub.api.routers.skills import router as skills_router
from contexthub.api.routers.tools import router as tools_router
from contexthub.config import Settings
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.generation.base import ContentGenerator
from contexthub.llm.factory import create_chat_client, create_embedding_client
from contexthub.retrieval.long_doc import (
    KeywordRetriever,
    LongDocRetrievalCoordinator,
    TreeRetriever,
)
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.context_service import ContextService
from contexthub.services.feedback_service import FeedbackService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_scheduler import LifecycleScheduler
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.memory_service import MemoryService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.services.skill_service import SkillService
from contexthub.services.document_ingester import LongDocumentIngester
from contexthub.store.context_store import ContextStore
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.propagation_engine import PropagationEngine
from contexthub.connectors.mock_connector import MockCatalogConnector
from contexthub.generation.table_schema import TableSchemaGenerator
from contexthub.services.catalog_sync_service import CatalogSyncService
from contexthub.services.masking_service import MaskingService
from contexthub.services.reconciler_service import ReconcilerService
from contexthub.services.share_service import ShareService
from contexthub.api.routers.datalake import router as datalake_router
from contexthub.api.routers.admin import router as admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_database_url,
        min_size=2,
        max_size=10,
        init=init_pg_connection,
    )
    embedding_client = None
    chat_client = None
    lifecycle_scheduler = None
    lifecycle_started = False
    propagation_engine = None
    propagation_started = False

    try:
        repo = PgRepository(pool)

        acl_service = ACLService()
        masking_service = MaskingService()
        audit_service = AuditService(pool=pool)
        share_service = ShareService(acl_service, audit=audit_service)

        # Task 3 services
        embedding_client = create_embedding_client(settings)
        chat_client = create_chat_client(settings)
        content_generator = ContentGenerator()
        indexer_service = IndexerService(
            content_generator,
            embedding_client,
            embedding_dimensions=settings.embedding_dimensions,
        )
        lifecycle_service = LifecycleService(audit=audit_service, indexer=indexer_service)
        context_store = ContextStore(
            acl_service,
            masking_service,
            audit=audit_service,
            lifecycle=lifecycle_service,
        )

        # Inject indexer into ContextService for embedding consistency
        context_service = ContextService(context_store, acl_service, indexer_service, audit=audit_service)
        memory_service = MemoryService(indexer_service, acl_service, masking_service, audit=audit_service)
        skill_service = SkillService(indexer_service, acl_service, masking_service, audit=audit_service)

        # Task 4: retrieval
        retrieval_router = RetrievalRouter.default()
        long_doc_coordinator = LongDocRetrievalCoordinator()
        long_doc_coordinator.register_strategy("tree", TreeRetriever(chat_client))
        long_doc_coordinator.register_strategy("keyword", KeywordRetriever(chat_client))
        retrieval_service = RetrievalService(
            retrieval_router, embedding_client, acl_service,
            masking_service=masking_service,
            audit_service=audit_service,
            long_doc_coordinator=long_doc_coordinator,
            over_retrieve_factor=settings.search_over_retrieve_factor,
        )
        document_ingester = LongDocumentIngester(
            chat_client=chat_client,
            embedding_client=embedding_client,
            content_generator=content_generator,
            acl=acl_service,
            audit=audit_service,
            doc_store_root=settings.doc_store_root,
            max_document_size_mb=settings.max_document_size_mb,
            max_token_per_node=settings.max_token_per_node,
        )
        feedback_service = FeedbackService(acl_service, audit=audit_service)

        app.state.settings = settings
        app.state.repo = repo
        app.state.acl_service = acl_service
        app.state.context_store = context_store
        app.state.context_service = context_service
        app.state.memory_service = memory_service
        app.state.skill_service = skill_service
        app.state.indexer_service = indexer_service
        app.state.lifecycle_service = lifecycle_service
        app.state.retrieval_service = retrieval_service
        app.state.long_doc_retrieval_coordinator = long_doc_coordinator
        app.state.masking_service = masking_service
        app.state.embedding_client = embedding_client
        app.state.chat_client = chat_client
        app.state.audit_service = audit_service
        app.state.share_service = share_service
        app.state.feedback_service = feedback_service
        app.state.document_ingester = document_ingester

        # Task 7: Carrier-specific services
        catalog_connector = MockCatalogConnector()
        table_schema_generator = TableSchemaGenerator()
        catalog_sync_service = CatalogSyncService(
            connector=catalog_connector,
            indexer=indexer_service,
            table_schema_generator=table_schema_generator,
        )
        reconciler_service = ReconcilerService(repo=repo, indexer=indexer_service)

        app.state.catalog_sync_service = catalog_sync_service
        app.state.reconciler_service = reconciler_service

        lifecycle_scheduler = LifecycleScheduler(
            lifecycle=lifecycle_service,
            repo=repo,
            pool=pool,
            interval_seconds=settings.lifecycle_sweep_interval,
        )
        app.state.lifecycle_scheduler = lifecycle_scheduler

        # Task 5: PropagationEngine
        rule_registry = PropagationRuleRegistry.default()
        propagation_engine = PropagationEngine(
            repo=repo,
            pool=pool,
            dsn=settings.asyncpg_database_url,
            rule_registry=rule_registry,
            lifecycle=lifecycle_service,
            indexer=indexer_service,
            sweep_interval=settings.propagation_sweep_interval,
            lease_timeout=settings.propagation_lease_timeout,
        )

        if settings.propagation_enabled:
            await propagation_engine.start()
            propagation_started = True
        if settings.lifecycle_enabled:
            await lifecycle_scheduler.start()
            lifecycle_started = True

        try:
            yield
        finally:
            if lifecycle_started:
                await lifecycle_scheduler.stop()
            if propagation_started:
                await propagation_engine.stop()
    finally:
        if chat_client is not None and hasattr(chat_client, "close"):
            await chat_client.close()
        if embedding_client is not None and hasattr(embedding_client, "close"):
            await embedding_client.close()
        await pool.close()


app = FastAPI(title="ContextHub", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(contexts_router)
app.include_router(memories_router)
app.include_router(feedback_router)
app.include_router(documents_router)
app.include_router(skills_router)
app.include_router(search_router)
app.include_router(tools_router)
app.include_router(datalake_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
