"""Tests for Task 4: RetrievalService, BM25 rerank, keyword fallback, embedding consistency."""

import uuid
from datetime import datetime, timezone

import pytest

from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.context import ContextLevel
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest
from contexthub.retrieval.keyword_strategy import keyword_search
from contexthub.retrieval.rerank import KeywordRerankStrategy
from contexthub.retrieval.router import RetrievalRouter
from contexthub.retrieval.vector_strategy import vector_search
from contexthub.services.acl_service import ACLService
from contexthub.services.feedback_service import QUALITY_MIN_SAMPLES
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

    def __init__(
        self,
        rows=None,
        l2_rows=None,
        quality_rows=None,
        invalidation_rows=None,
        upstream_rows=None,
    ):
        self._rows = rows or []
        self._l2_rows = l2_rows or []
        self._quality_rows = quality_rows or []
        # (context_id -> upstream_id) as returned by the unresolved-invalidation
        # join, and the upstream context rows it points at.
        self._invalidation_rows = invalidation_rows or []
        self._upstream_rows = upstream_rows or []
        self.executed = []
        self.fetches = []

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        if "context_invalidations" in sql:
            allowed = set(args[0])
            return [
                row for row in self._invalidation_rows if row["context_id"] in allowed
            ]
        # Upstream-node read: same column prefix as candidate search but no
        # scoring column, so it must be matched after the candidate branches.
        if (
            "SELECT id, uri, context_type, scope, owner_space, status," in sql
            and "cosine_similarity" not in sql
            and "LIKE" not in sql.upper()
        ):
            allowed = set(args[0])
            return [row for row in self._upstream_rows if row["id"] in allowed]
        if "SELECT id, version, status, validity_status" in sql:
            allowed = set(args[0])
            return [
                FakeRecord(
                    id=row["id"],
                    version=row.get("version"),
                    status=row.get("status"),
                    validity_status=row.get("validity_status")
                    or ("fresh" if row.get("status") == "active" else row.get("status")),
                )
                for row in self._rows
                if row["id"] in allowed
            ]
        if "SELECT id, version, l2_content" in sql:
            allowed = set(args[0])
            by_id = {row["id"]: row for row in self._rows}
            return [
                FakeRecord(
                    id=row["id"],
                    version=by_id[row["id"]].get("version"),
                    l2_content=row["l2_content"],
                )
                for row in self._l2_rows
                if row["id"] in allowed
            ]
        if "visible_teams" in sql:
            return [
                FakeRecord(path="engineering/backend"),
                FakeRecord(path="engineering"),
                FakeRecord(path=""),
            ]
        if "SELECT id, l2_content FROM contexts WHERE id IN" in sql:
            return self._l2_rows
        if "SELECT id, adopted_count, ignored_count" in sql:
            return self._quality_rows
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
    assert len(db.executed) == 2
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

    response = await svc.search(
        db,
        SearchRequest(query="database query", top_k=2, include_stale=True),
        ctx,
    )

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
    assert req.include_stale is False
    assert req.scope is None
    assert req.context_type is None


@pytest.mark.asyncio
async def test_search_quality_factor_promotes_high_quality_results():
    low_quality_id = uuid.uuid4()
    high_quality_id = uuid.uuid4()
    rows = [
        FakeRecord(
            id=low_quality_id,
            uri="ctx://team/engineering/resources/low-quality",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            status="active",
            version=1,
            l0_content="database indexing tips",
            l1_content="database indexing tips",
            tags=[],
            cosine_similarity=0.9,
        ),
        FakeRecord(
            id=high_quality_id,
            uri="ctx://team/engineering/resources/high-quality",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            status="active",
            version=1,
            l0_content="database indexing tips",
            l1_content="database indexing tips",
            tags=[],
            cosine_similarity=0.9,
        ),
    ]
    quality_rows = [
        FakeRecord(id=low_quality_id, adopted_count=0, ignored_count=8),
        FakeRecord(id=high_quality_id, adopted_count=8, ignored_count=0),
    ]
    db = SearchFlowDB(rows=rows, quality_rows=quality_rows)
    svc = _make_retrieval_service(NoOpEmbeddingClient())

    response = await svc.search(
        db,
        SearchRequest(query="database indexing", top_k=2),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert [result.uri for result in response.results] == [
        "ctx://team/engineering/resources/high-quality",
        "ctx://team/engineering/resources/low-quality",
    ]
    assert response.results[0].score > response.results[1].score


@pytest.mark.asyncio
async def test_search_quality_factor_preserves_low_sample_cold_start():
    cold_start_id = uuid.uuid4()
    peer_id = uuid.uuid4()
    rows = [
        FakeRecord(
            id=cold_start_id,
            uri="ctx://team/engineering/resources/cold-start",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            status="active",
            version=1,
            l0_content="python deployment checklist",
            l1_content="python deployment checklist",
            tags=[],
            cosine_similarity=0.9,
        ),
        FakeRecord(
            id=peer_id,
            uri="ctx://team/engineering/resources/peer",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            status="active",
            version=1,
            l0_content="python deployment checklist",
            l1_content="python deployment checklist",
            tags=[],
            cosine_similarity=0.9,
        ),
    ]
    quality_rows = [
        FakeRecord(
            id=cold_start_id,
            adopted_count=0,
            ignored_count=QUALITY_MIN_SAMPLES - 1,
        ),
        FakeRecord(id=peer_id, adopted_count=0, ignored_count=0),
    ]
    db = SearchFlowDB(rows=rows, quality_rows=quality_rows)
    svc = _make_retrieval_service(NoOpEmbeddingClient())

    response = await svc.search(
        db,
        SearchRequest(query="python deployment", top_k=2),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert [result.uri for result in response.results] == [
        "ctx://team/engineering/resources/cold-start",
        "ctx://team/engineering/resources/peer",
    ]
    assert response.results[0].score == pytest.approx(response.results[1].score)


@pytest.mark.asyncio
async def test_search_returns_non_empty_retrieval_id():
    row_id = uuid.uuid4()
    db = SearchFlowDB(
        rows=[
            FakeRecord(
                id=row_id,
                uri="ctx://team/engineering/resources/orders",
                context_type="resource",
                scope="team",
                owner_space="engineering",
                status="active",
                version=1,
                l0_content="orders runbook",
                l1_content="orders runbook",
                tags=[],
                cosine_similarity=0.7,
            ),
        ],
        quality_rows=[FakeRecord(id=row_id, adopted_count=0, ignored_count=0)],
    )
    svc = _make_retrieval_service(NoOpEmbeddingClient())

    response = await svc.search(
        db,
        SearchRequest(query="orders"),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert response.retrieval_id
    assert uuid.UUID(response.retrieval_id)


@pytest.mark.asyncio
async def test_default_search_filters_all_unserviceable_validity_states_with_trace():
    ids = {name: uuid.uuid4() for name in ("fresh", "stale", "superseded", "recomputing")}
    rows = [
        FakeRecord(
            id=ids[name],
            uri=name,
            context_type="memory",
            scope="datalake",
            owner_space=None,
            status="active" if name != "stale" else "stale",
            validity_status=name,
            validity_reason=f"{name} test",
            version=2,
            l0_content="shared retrieval phrase",
            l1_content="shared retrieval phrase",
            tags=[],
            cosine_similarity=0.9,
        )
        for name in ids
    ]
    db = SearchFlowDB(rows)
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="shared retrieval", top_k=10),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert [result.uri for result in response.results] == ["fresh"]
    filtered = {
        item["validity_status"]: item["filter_reasons"]
        for item in response.trace["candidates"]
        if item["filtered"]
    }
    assert "validity:stale" in filtered["stale"]
    assert "validity:superseded" in filtered["superseded"]
    assert "validity:recomputing" in filtered["recomputing"]
    assert len(response.trace["context_hash"]) == 64


def _stale_notice_fixture():
    """A stale node plus the immediate upstream that invalidated it."""
    stale_id = uuid.uuid4()
    upstream_id = uuid.uuid4()
    root_id = uuid.uuid4()
    rows = [
        FakeRecord(
            id=stale_id,
            uri="ctx://agent/eval/memories/cur-fitness_facility-aaa111",
            context_type="memory",
            scope="datalake",
            owner_space=None,
            status="stale",
            validity_status="stale",
            validity_reason="health_condition: lactose intolerance -> high blood pressure",
            version=4,
            l0_content="The user works out at Sunrise Gym.",
            l1_content="The user works out at Sunrise Gym.",
            tags=[],
            cosine_similarity=0.9,
        ),
    ]
    upstream_rows = [
        FakeRecord(
            id=upstream_id,
            uri="ctx://agent/eval/memories/cur-work_location-bbb222",
            context_type="memory",
            scope="datalake",
            owner_space=None,
            status="stale",
            version=2,
            l0_content="The user works at the Portland office.",
            l1_content="The user works at the Portland office.",
        )
    ]
    invalidation_rows = [
        FakeRecord(context_id=stale_id, upstream_id=upstream_id),
    ]
    return stale_id, upstream_id, root_id, rows, upstream_rows, invalidation_rows


@pytest.mark.asyncio
async def test_stale_notices_report_withheld_node_reason_and_nearest_upstream():
    stale_id, _, _, rows, upstream_rows, invalidation_rows = _stale_notice_fixture()
    fresh_id = uuid.uuid4()
    rows.append(
        FakeRecord(
            id=fresh_id,
            uri="fresh-note",
            context_type="memory",
            scope="datalake",
            owner_space=None,
            status="active",
            validity_status="fresh",
            validity_reason=None,
            version=1,
            l0_content="The user works out at Sunrise Gym on weekends.",
            l1_content="The user works out at Sunrise Gym on weekends.",
            tags=[],
            cosine_similarity=0.7,
        )
    )
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="works out", top_k=10, include_stale_notices=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    # The withheld node is still withheld: notices explain, they don't serve.
    assert [result.uri for result in response.results] == ["fresh-note"]
    assert len(response.stale_notices) == 1
    notice = response.stale_notices[0]
    assert notice.uri == rows[0]["uri"]
    assert notice.validity_status == "stale"
    assert notice.stale_content == "The user works out at Sunrise Gym."
    assert notice.reason == (
        "health_condition: lactose intolerance -> high blood pressure"
    )
    assert notice.source_uri == upstream_rows[0]["uri"]
    assert notice.source_content == "The user works at the Portland office."
    assert notice.version == 4
    assert str(stale_id) not in [result.uri for result in response.results]
    # Same payload is auditable in the trace.
    assert response.trace["stale_notices"][0]["source_uri"] == upstream_rows[0]["uri"]


@pytest.mark.asyncio
async def test_stale_notices_are_off_by_default():
    _, _, _, rows, upstream_rows, invalidation_rows = _stale_notice_fixture()
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="works out", top_k=10),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert response.results == []
    assert response.stale_notices == []
    # No invalidation lookup happens when notices were not asked for.
    assert not any("context_invalidations" in sql for sql, _ in db.fetches)


@pytest.mark.asyncio
async def test_stale_notices_suppressed_when_include_stale_serves_the_node():
    _, _, _, rows, upstream_rows, invalidation_rows = _stale_notice_fixture()
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(
            query="works out", include_stale=True, include_stale_notices=True
        ),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    # Nothing was withheld, so there is nothing to explain.
    assert [result.uri for result in response.results] == [rows[0]["uri"]]
    assert response.stale_notices == []


@pytest.mark.asyncio
async def test_stale_notice_falls_back_to_upstream_uri_for_generic_closure_reason():
    """Closure descendants get a reason naming a raw uuid; name the upstream."""
    _, upstream_id, root_id, rows, upstream_rows, invalidation_rows = (
        _stale_notice_fixture()
    )
    rows[0]["validity_status"] = "invalid"
    rows[0]["validity_reason"] = f"dependency closure invalidated by {root_id}"
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="works out", include_stale_notices=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    notice = response.stale_notices[0]
    assert str(root_id) not in (notice.reason or "")
    assert notice.reason == (
        f"an upstream note it depends on changed: {upstream_rows[0]['uri']}"
    )
    assert notice.source_uri == upstream_rows[0]["uri"]


@pytest.mark.asyncio
async def test_stale_notice_omits_upstream_when_none_is_recorded():
    _, _, _, rows, _, _ = _stale_notice_fixture()
    db = SearchFlowDB(rows, invalidation_rows=[], upstream_rows=[])
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="works out", include_stale_notices=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    notice = response.stale_notices[0]
    assert notice.source_uri is None
    assert notice.source_content is None
    # The rule's own recorded wording still survives.
    assert notice.reason == (
        "health_condition: lactose intolerance -> high blood pressure"
    )


@pytest.mark.asyncio
async def test_stale_notice_masks_content_under_field_masks(monkeypatch):
    _, _, _, rows, upstream_rows, invalidation_rows = _stale_notice_fixture()
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    service = _make_retrieval_service()

    async def fake_filter(db_, contexts, ctx):
        return [(c, ["Sunrise", "Portland"]) for c in contexts]

    monkeypatch.setattr(service._acl, "filter_visible_with_acl", fake_filter)
    response = await service.search(
        db,
        SearchRequest(query="works out", include_stale_notices=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    notice = response.stale_notices[0]
    assert "Sunrise" not in notice.stale_content
    assert "Portland" not in notice.source_content


@pytest.mark.asyncio
async def test_stale_notice_drops_node_the_caller_cannot_read(monkeypatch):
    _, _, _, rows, upstream_rows, invalidation_rows = _stale_notice_fixture()
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    service = _make_retrieval_service()

    async def deny_all(db_, contexts, ctx):
        return []

    monkeypatch.setattr(service._acl, "filter_visible_with_acl", deny_all)
    response = await service.search(
        db,
        SearchRequest(query="works out", include_stale_notices=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert response.stale_notices == []


@pytest.mark.asyncio
async def test_stale_notice_omits_upstream_the_caller_cannot_read(monkeypatch):
    """An invisible upstream is dropped from the notice, not leaked."""
    stale_id, _, _, rows, upstream_rows, invalidation_rows = _stale_notice_fixture()
    db = SearchFlowDB(
        rows, invalidation_rows=invalidation_rows, upstream_rows=upstream_rows
    )
    service = _make_retrieval_service()

    async def hide_upstream(db_, contexts, ctx):
        return [(c, None) for c in contexts if c["id"] == stale_id]

    monkeypatch.setattr(service._acl, "filter_visible_with_acl", hide_upstream)
    response = await service.search(
        db,
        SearchRequest(query="works out", include_stale_notices=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    notice = response.stale_notices[0]
    assert notice.source_uri is None
    assert notice.source_content is None


@pytest.mark.asyncio
async def test_debug_search_explicitly_includes_invalid_rows_with_status():
    row_id = uuid.uuid4()
    db = SearchFlowDB(
        [
            FakeRecord(
                id=row_id,
                uri="debug-stale",
                context_type="memory",
                scope="datalake",
                owner_space=None,
                status="stale",
                validity_status="invalid",
                validity_reason="upstream stale",
                version=3,
                l0_content="debug phrase",
                l1_content="debug phrase",
                tags=[],
                cosine_similarity=0.8,
            )
        ]
    )
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="debug phrase", include_stale=True),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert response.results[0].validity_status == "invalid"
    assert response.trace["unsafe_debug_read"] is True


@pytest.mark.asyncio
async def test_concurrent_invalidation_between_candidate_and_materialization_is_filtered():
    row_id = uuid.uuid4()

    class ConcurrentInvalidationDB(SearchFlowDB):
        async def fetch(self, sql, *args):
            if "SELECT id, version, status, validity_status" in sql:
                return [
                    FakeRecord(
                        id=row_id,
                        version=1,
                        status="active",
                        validity_status="invalid",
                    )
                ]
            return await super().fetch(sql, *args)

    db = ConcurrentInvalidationDB(
        [
            FakeRecord(
                id=row_id,
                uri="racy",
                context_type="memory",
                scope="datalake",
                owner_space=None,
                status="active",
                validity_status="fresh",
                version=1,
                l0_content="race phrase",
                l1_content="race phrase",
                tags=[],
                cosine_similarity=0.9,
            )
        ]
    )
    response = await _make_retrieval_service().search(
        db,
        SearchRequest(query="race phrase"),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert response.results == []
    assert "materialization_validity:invalid" in response.trace["candidates"][0][
        "filter_reasons"
    ]


class QueryCaptureDB:
    def __init__(self):
        self.fetches = []

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return []


class QueryResultDB(QueryCaptureDB):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return self._rows


@pytest.mark.asyncio
async def test_vector_search_excludes_archived_and_deleted_statuses():
    db = QueryCaptureDB()

    await vector_search(db, [0.1, 0.2, 0.3], 5)

    assert db.fetches
    assert "status NOT IN ('archived', 'deleted')" in db.fetches[0][0]


@pytest.mark.asyncio
async def test_keyword_search_excludes_archived_and_deleted_statuses():
    db = QueryCaptureDB()

    await keyword_search(db, "orders table", 5)

    assert db.fetches
    assert "status NOT IN ('archived', 'deleted')" in db.fetches[0][0]


@pytest.mark.asyncio
async def test_vector_search_returns_file_path_in_candidates():
    ctx_id = uuid.uuid4()
    db = QueryResultDB(
        [
            FakeRecord(
                id=ctx_id,
                uri="ctx://resources/manuals/postgres",
                context_type="resource",
                scope="team",
                owner_space="engineering",
                status="active",
                version=1,
                l0_content="postgres handbook",
                l1_content="replication guide",
                tags=[],
                file_path="/tmp/postgres-doc",
                cosine_similarity=0.8,
            )
        ]
    )

    results = await vector_search(db, [0.1, 0.2, 0.3], 5)

    assert results[0]["file_path"] == "/tmp/postgres-doc"


@pytest.mark.asyncio
async def test_keyword_search_returns_file_path_in_candidates():
    ctx_id = uuid.uuid4()
    db = QueryResultDB(
        [
            FakeRecord(
                id=ctx_id,
                uri="ctx://resources/manuals/postgres",
                context_type="resource",
                scope="team",
                owner_space="engineering",
                status="active",
                version=1,
                l0_content="postgres handbook",
                l1_content="replication guide",
                tags=[],
                file_path="/tmp/postgres-doc",
                cosine_similarity=0.5,
            )
        ]
    )

    results = await keyword_search(db, "postgres", 5)

    assert results[0]["file_path"] == "/tmp/postgres-doc"
