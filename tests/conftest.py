"""Shared pytest fixtures for integration tests.

Integration tests require a running PostgreSQL instance (docker-compose up).
Gate with CONTEXTHUB_INTEGRATION=1 environment variable.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

# Skip all integration tests unless explicitly enabled
_INTEGRATION = os.environ.get("CONTEXTHUB_INTEGRATION", "").strip()
if not _INTEGRATION or _INTEGRATION == "0":
    collect_ignore_glob = ["test_integration_*.py"]


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless CONTEXTHUB_INTEGRATION=1."""
    if os.environ.get("CONTEXTHUB_INTEGRATION", "").strip() in ("1", "true"):
        return
    skip_marker = pytest.mark.skip(reason="CONTEXTHUB_INTEGRATION not set")
    for item in items:
        # Skip test_integration_* files and DB-backed tests in test_datalake.py
        if "integration" in item.nodeid:
            item.add_marker(skip_marker)
        elif "datalake" in item.nodeid and _needs_db(item):
            item.add_marker(skip_marker)


def _needs_db(item) -> bool:
    """Check if a test function uses DB fixtures (acme_session, services, etc.)."""
    # If the test uses fixtures that require DB, skip it
    if hasattr(item, "fixturenames"):
        db_fixtures = {"acme_session", "services", "db_pool", "repo", "clean_db"}
        return bool(db_fixtures & set(item.fixturenames))
    return False


@pytest_asyncio.fixture
async def db_pool():
    """Create asyncpg pool connected to test database."""
    import asyncpg
    pool = await asyncpg.create_pool(
        "postgresql://contexthub:contexthub@localhost:5432/contexthub",
        min_size=1, max_size=5,
    )
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def repo(db_pool):
    """PgRepository instance."""
    from contexthub.db.repository import PgRepository
    return PgRepository(db_pool)


@pytest_asyncio.fixture
async def clean_db(db_pool):
    """Truncate business data before each test, preserving seed data."""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            TRUNCATE contexts, dependencies, change_events,
                     table_metadata, lineage, table_relationships,
                     query_templates, skill_versions, skill_subscriptions
            CASCADE
        """)
    yield


@pytest_asyncio.fixture
async def acme_session(repo, clean_db):
    """ScopedRepo with account_id='acme'."""
    async with repo.session("acme") as db:
        yield db


@pytest_asyncio.fixture
def query_agent_ctx():
    from contexthub.models.request import RequestContext
    return RequestContext(account_id="acme", agent_id="query-agent")


@pytest_asyncio.fixture
def analysis_agent_ctx():
    from contexthub.models.request import RequestContext
    return RequestContext(account_id="acme", agent_id="analysis-agent")


@pytest_asyncio.fixture
def services(repo):
    """All service instances wired together."""
    from contexthub.generation.base import ContentGenerator
    from contexthub.generation.table_schema import TableSchemaGenerator
    from contexthub.llm.base import NoOpEmbeddingClient
    from contexthub.services.acl_service import ACLService
    from contexthub.services.indexer_service import IndexerService
    from contexthub.services.memory_service import MemoryService
    from contexthub.services.skill_service import SkillService
    from contexthub.services.retrieval_service import RetrievalService
    from contexthub.services.catalog_sync_service import CatalogSyncService
    from contexthub.services.reconciler_service import ReconcilerService
    from contexthub.connectors.mock_connector import MockCatalogConnector
    from contexthub.retrieval.router import RetrievalRouter
    from contexthub.propagation.registry import PropagationRuleRegistry
    from contexthub.store.context_store import ContextStore

    from contexthub.services.masking_service import MaskingService

    acl = ACLService()
    masking = MaskingService()
    context_store = ContextStore(acl, masking)
    embedding = NoOpEmbeddingClient()
    generator = ContentGenerator()
    indexer = IndexerService(generator, embedding)
    memory = MemoryService(indexer, acl, masking)
    skill = SkillService(indexer, acl, masking)
    retrieval_router = RetrievalRouter.default()
    retrieval = RetrievalService(
        retrieval_router, embedding, acl,
        masking_service=masking,
    )
    catalog_connector = MockCatalogConnector()
    table_gen = TableSchemaGenerator()
    catalog_sync = CatalogSyncService(catalog_connector, indexer, table_gen)
    reconciler = ReconcilerService(repo, indexer)
    rule_registry = PropagationRuleRegistry.default()

    class _Services:
        pass

    s = _Services()
    s.acl = acl
    s.masking = masking
    s.context_store = context_store
    s.indexer = indexer
    s.memory = memory
    s.skill = skill
    s.retrieval = retrieval
    s.catalog_sync = catalog_sync
    s.catalog_connector = catalog_connector
    s.reconciler = reconciler
    s.rule_registry = rule_registry
    s.repo = repo
    return s
