"""Tests for Task 4: RetrievalService, BM25 rerank, keyword fallback, embedding consistency."""

import uuid
from datetime import datetime, timezone

import pytest

from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.context import ContextLevel
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest
from contexthub.retrieval.rerank import KeywordRerankStrategy
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService


_NOW = datetime.now(timezone.utc)


class FakeRecord(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


# --- Mock Embedding Client ---

class MockEmbeddingClient:
    """Returns deterministic embeddings for testing."""

    async def embed(self, text: str) -> list[float] | None:
        # Simple deterministic embedding: hash-based
        if "database" in text.lower() or "sql" in text.lower():
            return [1.0] + [0.0] * 1535
        if "python" in text.lower():
            return [0.0, 1.0] + [0.0] * 1534
        return [0.5] * 1536

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        return [await self.embed(t) for t in texts]

    async def close(self):
        pass


class WrongDimensionEmbeddingClient:
    async def embed(self, text: str) -> list[float] | None:
        return [1.0, 2.0]


# --- Fake DB for keyword search ---

class SearchFlowDB:
    """Simulates DB interactions for RetrievalService tests."""

    def __init__(self, rows=None, l2_rows=None):
        self._rows = rows or []
        self._l2_rows = l2_rows or []
        self.executed = []

    async def fetch(self, sql, *args):
        if "visible_teams" in sql:
            return [
                FakeRecord(path="engineering/backend"),
                FakeRecord(path="engineering"),
                FakeRecord(path=""),
            ]
        if "SELECT id, l2_content FROM contexts WHERE id IN" in sql:
            return self._l2_rows
        if "cosine_similarity" in sql or "LIKE" in sql.upper():
            return self._rows
        if "access_policies" in sql:
            return []
        if "team_memberships" in sql:
            return [
                FakeRecord(path="engineering/backend"),
                FakeRecord(path="engineering"),
            ]
        raise AssertionError(f"Unexpected fetch: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 0"

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None


# --- BM25 Rerank Tests ---

@pytest.mark.asyncio
async def test_bm25_rerank_orders_by_keyword_relevance():
    strategy = KeywordRerankStrategy()
    candidates = [
        {"l1_content": "This is about cats and dogs", "uri": "a"},
        {"l1_content": "Database optimization and SQL tuning for databases", "uri": "b"},
        {"l1_content": "SQL query performance in database systems", "uri": "c"},
    ]

    result = await strategy.rerank("database SQL optimization", candidates)

    # b and c should rank higher than a (they contain query keywords)
    uris = [r["uri"] for r in result]
    assert uris.index("a") > uris.index("b")
    assert uris.index("a") > uris.index("c")


@pytest.mark.asyncio
async def test_bm25_rerank_empty_candidates():
    strategy = KeywordRerankStrategy()
    result = await strategy.rerank("test query", [])
    assert result == []


@pytest.mark.asyncio
async def test_bm25_rerank_empty_query():
    strategy = KeywordRerankStrategy()
    candidates = [{"l1_content": "some content", "uri": "a"}]
    result = await strategy.rerank("", candidates)
    assert len(result) == 1


# --- RetrievalService with keyword fallback ---

def _make_retrieval_service(embedding_client=None):
    router = RetrievalRouter.default()
    client = embedding_client or NoOpEmbeddingClient()
    acl = ACLService()
    masking = MaskingService()
    return RetrievalService(
        router, client, acl,
        masking_service=masking,
        over_retrieve_factor=3,
    )


@pytest.mark.asyncio
async def test_keyword_fallback_returns_visible_results_and_updates_active_count():
    visible_id = uuid.uuid4()
    hidden_id = uuid.uuid4()
    rows = [
        FakeRecord(
            id=visible_id, uri="ctx://datalake/prod/orders",
            context_type="table_schema", scope="datalake", owner_space=None,
            status="active", version=1,
            l0_content="Orders table schema",
            l1_content="Orders table with columns: id, customer_id, total, created_at",
            tags=[], cosine_similarity=0.5,
        ),
        FakeRecord(
            id=hidden_id, uri="ctx://agent/other-agent/memories/orders",
            context_type="memory", scope="agent", owner_space="other-agent",
            status="active", version=1,
            l0_content="Orders private note",
            l1_content="Orders table issue private note",
            tags=[], cosine_similarity=0.4,
        ),
    ]
    db = SearchFlowDB(rows)
    svc = _make_retrieval_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    request = SearchRequest(query="orders table")

    response = await svc.search(db, request, ctx)

    assert response.total == 1
    assert response.results[0].uri == "ctx://datalake/prod/orders"
    assert len(db.executed) == 1
    assert "active_count = active_count + 1" in db.executed[0][0]
    assert db.executed[0][1][0] == [visible_id]


# --- Stale / Archived semantics ---

@pytest.mark.asyncio
async def test_search_penalizes_stale_results_after_rerank():
    stale_id = uuid.uuid4()
    active_id = uuid.uuid4()
    rows = [
        {"l1_content": "database query optimization", "uri": "active", "status": "active",
         "scope": "datalake", "owner_space": None, "id": active_id,
         "context_type": "table_schema", "version": 1, "l0_content": "database query optimization",
         "tags": [], "cosine_similarity": 0.9},
        {"l1_content": "database query optimization", "uri": "stale", "status": "stale",
         "scope": "datalake", "owner_space": None, "id": stale_id,
         "context_type": "table_schema", "version": 1, "l0_content": "database query optimization",
         "tags": [], "cosine_similarity": 0.9},
    ]
    # stale row comes first from retrieval so the test proves penalty reshuffles it
    db = SearchFlowDB([FakeRecord(**rows[1]), FakeRecord(**rows[0])])
    svc = _make_retrieval_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    response = await svc.search(db, SearchRequest(query="database query", top_k=2), ctx)

    assert [r.uri for r in response.results] == ["active", "stale"]
    assert response.results[0].score > response.results[1].score


@pytest.mark.asyncio
async def test_search_level_l2_loads_l2_content_for_final_results():
    row_id = uuid.uuid4()
    db = SearchFlowDB(
        rows=[
            FakeRecord(
                id=row_id, uri="ctx://datalake/prod/orders",
                context_type="table_schema", scope="datalake", owner_space=None,
                status="active", version=1,
                l0_content="Orders table schema",
                l1_content="Orders table with columns",
                tags=[], cosine_similarity=0.6,
            ),
        ],
        l2_rows=[FakeRecord(id=row_id, l2_content="CREATE TABLE orders (...);")],
    )
    svc = _make_retrieval_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    response = await svc.search(
        db,
        SearchRequest(query="orders", level=ContextLevel.L2),
        ctx,
    )

    assert response.total == 1
    assert response.results[0].l2_content == "CREATE TABLE orders (...);"


# --- IndexerService embedding methods ---

class EmbeddingWriteDB:
    def __init__(self):
        self.updates = []
        self.clears = []
        self._rows = []

    async def execute(self, sql, *args):
        if "l0_embedding = $1::vector" in sql:
            self.updates.append(args)
        elif "l0_embedding = NULL" in sql:
            self.clears.append(args)
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        return self._rows

    def set_backfill_rows(self, rows):
        self._rows = rows


@pytest.mark.asyncio
async def test_update_embedding_writes_vector():
    client = MockEmbeddingClient()
    indexer = IndexerService(ContentGenerator(), client, embedding_dimensions=1536)
    db = EmbeddingWriteDB()
    ctx_id = uuid.uuid4()

    success = await indexer.update_embedding(db, ctx_id, "database schema")

    assert success is True
    assert len(db.updates) == 1
    assert db.updates[0][1] == ctx_id


@pytest.mark.asyncio
async def test_update_embedding_returns_false_on_noop():
    indexer = IndexerService(ContentGenerator(), NoOpEmbeddingClient(), embedding_dimensions=1536)
    db = EmbeddingWriteDB()

    success = await indexer.update_embedding(db, uuid.uuid4(), "test")

    assert success is False
    assert len(db.updates) == 0


@pytest.mark.asyncio
async def test_clear_embedding():
    indexer = IndexerService(ContentGenerator(), NoOpEmbeddingClient(), embedding_dimensions=1536)
    db = EmbeddingWriteDB()
    ctx_id = uuid.uuid4()

    await indexer.clear_embedding(db, ctx_id)

    assert len(db.clears) == 1
    assert db.clears[0][0] == ctx_id


@pytest.mark.asyncio
async def test_backfill_embeddings():
    client = MockEmbeddingClient()
    indexer = IndexerService(ContentGenerator(), client, embedding_dimensions=1536)
    db = EmbeddingWriteDB()

    row1_id = uuid.uuid4()
    row2_id = uuid.uuid4()
    db.set_backfill_rows([
        FakeRecord(id=row1_id, l0_content="database schema"),
        FakeRecord(id=row2_id, l0_content="python code"),
    ])

    count = await indexer.backfill_embeddings(db, batch_size=10)

    assert count == 2
    assert len(db.updates) == 2


@pytest.mark.asyncio
async def test_backfill_with_noop_returns_zero():
    indexer = IndexerService(ContentGenerator(), NoOpEmbeddingClient(), embedding_dimensions=1536)
    db = EmbeddingWriteDB()
    db.set_backfill_rows([])

    count = await indexer.backfill_embeddings(db)

    assert count == 0


@pytest.mark.asyncio
async def test_update_embedding_returns_false_on_dimension_mismatch():
    indexer = IndexerService(
        ContentGenerator(),
        WrongDimensionEmbeddingClient(),
        embedding_dimensions=1536,
    )
    db = EmbeddingWriteDB()

    success = await indexer.update_embedding(db, uuid.uuid4(), "database schema")

    assert success is False
    assert len(db.updates) == 0


# --- RetrievalRouter ---

def test_retrieval_router_default():
    router = RetrievalRouter.default()
    assert isinstance(router.rerank, KeywordRerankStrategy)


# --- SearchRequest / SearchResponse models ---

def test_search_request_defaults():
    req = SearchRequest(query="test")
    assert req.top_k == 10
    assert req.level == ContextLevel.L1
    assert req.include_stale is True
    assert req.scope is None
    assert req.context_type is None
