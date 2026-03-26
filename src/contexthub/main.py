"""Application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from contexthub.api.middleware import AuthMiddleware
from contexthub.api.routers.contexts import router as contexts_router
from contexthub.api.routers.memories import router as memories_router
from contexthub.api.routers.search import router as search_router
from contexthub.api.routers.skills import router as skills_router
from contexthub.api.routers.tools import router as tools_router
from contexthub.config import Settings
from contexthub.db.pool import create_pool
from contexthub.db.repository import PgRepository
from contexthub.generation.base import ContentGenerator
from contexthub.llm.factory import create_embedding_client
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.context_service import ContextService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.memory_service import MemoryService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.services.skill_service import SkillService
from contexthub.store.context_store import ContextStore
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.propagation_engine import PropagationEngine
from contexthub.connectors.mock_connector import MockCatalogConnector
from contexthub.generation.table_schema import TableSchemaGenerator
from contexthub.services.catalog_sync_service import CatalogSyncService
from contexthub.services.reconciler_service import ReconcilerService
from contexthub.api.routers.datalake import router as datalake_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    pool = await create_pool(settings)
    embedding_client = None
    propagation_engine = None
    propagation_started = False

    try:
        repo = PgRepository(pool)

        acl_service = ACLService()
        context_store = ContextStore(acl_service)

        # Task 3 services
        embedding_client = create_embedding_client(settings)
        content_generator = ContentGenerator()
        indexer_service = IndexerService(
            content_generator,
            embedding_client,
            embedding_dimensions=settings.embedding_dimensions,
        )

        # Inject indexer into ContextService for embedding consistency
        context_service = ContextService(context_store, acl_service, indexer_service)
        memory_service = MemoryService(indexer_service, acl_service)
        skill_service = SkillService(indexer_service, acl_service)

        # Task 4: retrieval
        retrieval_router = RetrievalRouter.default()
        retrieval_service = RetrievalService(
            retrieval_router, embedding_client, acl_service,
            over_retrieve_factor=settings.search_over_retrieve_factor,
        )

        app.state.settings = settings
        app.state.repo = repo
        app.state.acl_service = acl_service
        app.state.context_store = context_store
        app.state.context_service = context_service
        app.state.memory_service = memory_service
        app.state.skill_service = skill_service
        app.state.indexer_service = indexer_service
        app.state.retrieval_service = retrieval_service
        app.state.embedding_client = embedding_client

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

        # Task 5: PropagationEngine
        rule_registry = PropagationRuleRegistry.default()
        propagation_engine = PropagationEngine(
            repo=repo,
            pool=pool,
            dsn=settings.asyncpg_database_url,
            rule_registry=rule_registry,
            indexer=indexer_service,
            sweep_interval=settings.propagation_sweep_interval,
            lease_timeout=settings.propagation_lease_timeout,
        )

        if settings.propagation_enabled:
            await propagation_engine.start()
            propagation_started = True

        try:
            yield
        finally:
            if propagation_started:
                await propagation_engine.stop()
    finally:
        if embedding_client is not None and hasattr(embedding_client, "close"):
            await embedding_client.close()
        await pool.close()


app = FastAPI(title="ContextHub", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(contexts_router)
app.include_router(memories_router)
app.include_router(skills_router)
app.include_router(search_router)
app.include_router(tools_router)
app.include_router(datalake_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
