"""Carrier-specific tests: MockCatalogConnector + CatalogSyncService + sql-context."""

import importlib
import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from contexthub.connectors.mock_connector import MockCatalogConnector
from contexthub.connectors.base import CatalogChange
from contexthub.api.routers.datalake import (
    SqlContextRequest,
    get_table_detail,
    search_sql_context,
)
from contexthub.generation.table_schema import TableSchemaGenerator
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchResponse, SearchResult
from contexthub.services.access_decision import AccessDecision
from contexthub.services.masking_service import MaskingService


# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_connector_list_databases():
    c = MockCatalogConnector()
    dbs = await c.list_databases()
    assert "prod" in dbs


@pytest.mark.asyncio
async def test_mock_connector_list_tables():
    c = MockCatalogConnector()
    tables = await c.list_tables("prod")
    assert set(tables) >= {"users", "orders", "products", "order_items", "payments"}


@pytest.mark.asyncio
async def test_mock_connector_get_schema():
    c = MockCatalogConnector()
    schema = await c.get_table_schema("prod", "orders")
    assert schema.ddl
    assert len(schema.columns) >= 3
    assert schema.comment


@pytest.mark.asyncio
async def test_mock_connector_get_sample_data():
    c = MockCatalogConnector()
    data = await c.get_sample_data("prod", "users", limit=2)
    assert len(data) == 2


@pytest.mark.asyncio
async def test_mock_connector_relationships():
    c = MockCatalogConnector()
    rels = await c.get_relationships()
    assert len(rels) >= 4
    from_tables = {r.from_table for r in rels}
    assert "orders" in from_tables
    assert "order_items" in from_tables


@pytest.mark.asyncio
async def test_mock_connector_inject_change():
    from datetime import datetime, timezone
    c = MockCatalogConnector()
    change = CatalogChange(database="prod", table="users", change_type="schema_changed")
    c.inject_change(change)
    changes = await c.detect_changes(since=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert len(changes) == 1
    assert changes[0].table == "users"


def test_table_schema_generator_l0():
    from contexthub.connectors.base import TableSchema
    gen = TableSchemaGenerator()
    schema = TableSchema(
        database="prod", table="orders",
        ddl="CREATE TABLE orders ...",
        columns=[
            {"name": "id", "type": "BIGINT", "comment": "订单ID"},
            {"name": "user_id", "type": "BIGINT", "comment": "用户ID"},
            {"name": "total_amount", "type": "DECIMAL", "comment": "总额"},
        ],
        comment="订单主表",
    )
    result = gen.generate_from_schema(schema)
    assert "orders" in result.l0
    assert len(result.l0) <= 80
    assert "| 字段 |" in result.l1
    assert "BIGINT" in result.l1


# ---------------------------------------------------------------------------
# Regression: search_sql_context must drop rows when post-fetch ACL denies
# ---------------------------------------------------------------------------


class _FakeRecord(dict):
    """Dict subclass that also supports attribute-style access for asyncpg compat."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


@pytest.mark.asyncio
async def test_search_sql_context_drops_denied_rows_on_post_fetch_acl():
    """P1 regression: if the second check_read_access() denies a URI that
    the initial retrieval.search() allowed, the row MUST be dropped — not
    returned with empty masks (fail-open)."""

    allowed_id = str(uuid.uuid4())
    denied_id = str(uuid.uuid4())
    allowed_uri = "ctx://datalake/mock/prod/users"
    denied_uri = "ctx://datalake/mock/prod/orders"

    class _StubRetrieval:
        async def search(self, db, request, ctx):
            return SearchResponse(
                results=[
                    SearchResult(
                        uri=allowed_uri, context_type="table_schema",
                        scope="datalake", score=0.95,
                        status="active", version=1,
                    ),
                    SearchResult(
                        uri=denied_uri, context_type="table_schema",
                        scope="datalake", score=0.90,
                        status="active", version=1,
                    ),
                ],
                total=2,
            )

    class _PostFetchDenyACL:
        """Allow one URI, deny the other on the second ACL evaluation."""
        async def check_read_access(self, db, uri, ctx):
            if uri == allowed_uri:
                return AccessDecision(allowed=True, field_masks=None, reason="ok")
            return AccessDecision(allowed=False, field_masks=None, reason="policy changed")

    _fetchrow_map = {
        allowed_uri: _FakeRecord(id=allowed_id),
        denied_uri: _FakeRecord(id=denied_id),
    }

    _fetch_rows = [
        _FakeRecord(
            id=allowed_id, uri=allowed_uri,
            l0_content="users table", l1_content="users detail",
            ddl="CREATE TABLE users ...", partition_info=None,
            sample_data=None, joins=None, top_templates=None,
        ),
        _FakeRecord(
            id=denied_id, uri=denied_uri,
            l0_content="orders table", l1_content="orders detail",
            ddl="CREATE TABLE orders ...", partition_info=None,
            sample_data=None, joins=None, top_templates=None,
        ),
    ]

    class _FakeDB:
        async def fetchrow(self, sql, *args):
            uri = args[0]
            return _fetchrow_map.get(uri)

        async def fetch(self, sql, *args):
            return _fetch_rows

    resp = await search_sql_context(
        SqlContextRequest(query="user orders", catalog="mock", top_k=5),
        ctx=RequestContext(account_id="acme", agent_id="query-agent"),
        db=_FakeDB(),
        retrieval=_StubRetrieval(),
        acl=_PostFetchDenyACL(),
        masking=MaskingService(),
    )

    returned_uris = [t.uri for t in resp.tables]
    assert allowed_uri in returned_uris, "allowed URI must be present"
    assert denied_uri not in returned_uris, (
        "denied URI must be DROPPED, not returned with empty masks (fail-open)"
    )
    assert resp.total_tables_found == 1


@pytest.mark.asyncio
async def test_search_sql_context_masks_fields_when_acl_returns_field_masks():
    """Post-fetch ACL returns allow + field_masks → l0, l1, ddl, sample_data
    must have the keyword replaced; structural fields left untouched."""

    ctx_id = str(uuid.uuid4())
    uri = "ctx://datalake/mock/prod/users"

    class _StubRetrieval:
        async def search(self, db, request, ctx):
            return SearchResponse(
                results=[
                    SearchResult(
                        uri=uri, context_type="table_schema",
                        scope="datalake", score=0.95,
                        status="active", version=1,
                    ),
                ],
                total=1,
            )

    class _MaskingACL:
        async def check_read_access(self, db, uri, ctx):
            return AccessDecision(
                allowed=True,
                field_masks=["salary"],
                reason="mask salary",
            )

    class _FakeDB:
        async def fetchrow(self, sql, *args):
            return _FakeRecord(id=ctx_id)

        async def fetch(self, sql, *args):
            return [
                _FakeRecord(
                    id=ctx_id, uri=uri,
                    l0_content="users table with salary column",
                    l1_content="| salary | DECIMAL | 薪资 |",
                    ddl="CREATE TABLE users (id BIGINT, salary DECIMAL)",
                    partition_info=None,
                    sample_data=[
                        {"id": 1, "name": "Alice", "salary": 80000},
                        {"id": 2, "name": "Bob", "salary": 95000},
                    ],
                    joins=None,
                    top_templates=None,
                ),
            ]

    resp = await search_sql_context(
        SqlContextRequest(
            query="user salary", catalog="mock", top_k=5,
            include_sample_data=True,
        ),
        ctx=RequestContext(account_id="acme", agent_id="query-agent"),
        db=_FakeDB(),
        retrieval=_StubRetrieval(),
        acl=_MaskingACL(),
        masking=MaskingService(),
    )

    assert len(resp.tables) == 1
    t = resp.tables[0]

    assert "salary" not in t.l0_content.lower()
    assert "[MASKED]" in t.l0_content

    assert "salary" not in t.l1_content.lower()
    assert "[MASKED]" in t.l1_content

    assert "salary" not in t.ddl.lower()
    assert "[MASKED]" in t.ddl

    assert t.sample_data is not None
    for row in t.sample_data:
        assert row["salary"] == "[MASKED]", "sample_data value for masked key must be replaced"
        assert row["name"] != "[MASKED]", "non-masked keys must be untouched"


@pytest.mark.asyncio
async def test_get_table_detail_masks_text_and_sample_data():
    """get_table_detail() must mask l0/l1/l2/ddl via text regex AND
    sample_data via exact key matching when field_masks is non-empty."""

    class _MaskingACL:
        async def check_read_access(self, db, uri, ctx):
            return AccessDecision(
                allowed=True,
                field_masks=["salary"],
                reason="mask salary",
            )

    _detail = {
        "l0_content": "users — contains salary info",
        "l1_content": "| salary | DECIMAL | 薪资 |",
        "l2_content": "The salary column stores gross annual pay.",
        "ddl": "CREATE TABLE users (id BIGINT, salary DECIMAL, monthly_salary DECIMAL)",
        "sample_data": [
            {"id": 1, "name": "Alice", "salary": 80000, "monthly_salary": 6666},
        ],
    }

    class _StubSvc:
        async def get_table_detail(self, db, catalog, database, table):
            return dict(_detail)

    resp = await get_table_detail(
        catalog="mock", database="prod", table="users",
        ctx=RequestContext(account_id="acme", agent_id="query-agent"),
        db=None,
        svc=_StubSvc(),
        acl=_MaskingACL(),
        masking=MaskingService(),
    )

    assert "salary" not in resp["l0_content"]
    assert "[MASKED]" in resp["l0_content"]

    assert "[MASKED]" in resp["l1_content"]

    assert "[MASKED]" in resp["l2_content"]

    assert "[MASKED]" in resp["ddl"]
    assert "monthly_[MASKED]" in resp["ddl"], (
        "text-level regex is substring-based: 'salary' inside 'monthly_salary' is replaced"
    )

    sd = resp["sample_data"]
    assert sd[0]["salary"] == "[MASKED]", "exact key match must mask"
    assert sd[0]["monthly_salary"] == 6666, (
        "JSON key masking is exact-match: 'salary' must NOT match 'monthly_salary' key"
    )
    assert sd[0]["name"] == "Alice", "non-masked keys untouched"


# ---------------------------------------------------------------------------
# Integration tests (require PG, gated by CONTEXTHUB_INTEGRATION)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_all_creates_contexts(acme_session, services):
    """sync_all populates contexts + table_metadata for all mock tables."""
    result = await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    assert result.tables_synced >= 5
    assert result.tables_created >= 5
    assert not result.errors

    # Verify contexts exist
    count = await acme_session.fetchval(
        "SELECT COUNT(*) FROM contexts WHERE context_type = 'table_schema'"
    )
    assert count >= 5


@pytest.mark.asyncio
async def test_sync_all_writes_table_metadata(acme_session, services):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    row = await acme_session.fetchrow(
        """
        SELECT tm.* FROM table_metadata tm
        JOIN contexts c ON c.id = tm.context_id
        WHERE c.uri = 'ctx://datalake/mock/prod/orders'
        """
    )
    assert row is not None
    assert row["ddl"] is not None
    assert row["catalog"] == "mock"
    assert row["table_name"] == "orders"


@pytest.mark.asyncio
async def test_sync_all_writes_l0_l1(acme_session, services):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    row = await acme_session.fetchrow(
        "SELECT l0_content, l1_content FROM contexts WHERE uri = 'ctx://datalake/mock/prod/orders'"
    )
    assert row["l0_content"]
    assert "orders" in row["l0_content"]
    assert row["l1_content"]


@pytest.mark.asyncio
async def test_sync_all_writes_relationships(acme_session, services):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    count = await acme_session.fetchval("SELECT COUNT(*) FROM table_relationships")
    assert count >= 4


@pytest.mark.asyncio
async def test_sync_idempotent(acme_session, services):
    """Repeated sync_all should not duplicate rows."""
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    count1 = await acme_session.fetchval(
        "SELECT COUNT(*) FROM contexts WHERE context_type = 'table_schema'"
    )
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    count2 = await acme_session.fetchval(
        "SELECT COUNT(*) FROM contexts WHERE context_type = 'table_schema'"
    )
    assert count1 == count2


@pytest.mark.asyncio
async def test_ddl_change_triggers_change_event(acme_session, services):
    """DDL change on re-sync should insert a change_event."""
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")

    # Manually change DDL in table_metadata to simulate schema drift
    await acme_session.execute(
        """
        UPDATE table_metadata SET ddl = 'CREATE TABLE orders (id BIGINT, new_col TEXT)'
        WHERE table_name = 'orders'
        """
    )

    # Re-sync — the connector still returns original DDL, so it differs
    await services.catalog_sync.sync_table(acme_session, "mock", "prod", "orders", "acme")

    events = await acme_session.fetch(
        """
        SELECT * FROM change_events
        WHERE change_type = 'modified' AND actor = 'catalog_sync'
        """
    )
    assert len(events) >= 1


@pytest.mark.asyncio
async def test_stats_only_update_no_change_event(acme_session, services):
    """Stats-only update should NOT create change_events."""
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")

    # Count events after initial sync
    initial_count = await acme_session.fetchval("SELECT COUNT(*) FROM change_events")

    # Re-sync same table (DDL unchanged) — should not add events
    await services.catalog_sync.sync_table(acme_session, "mock", "prod", "orders", "acme")

    final_count = await acme_session.fetchval("SELECT COUNT(*) FROM change_events")
    assert final_count == initial_count


@pytest.mark.asyncio
async def test_lineage_written(acme_session, services):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    rows = await acme_session.fetch("SELECT * FROM lineage")
    assert len(rows) >= 4


@pytest.mark.asyncio
async def test_archived_table_hidden_from_list_and_detail(acme_session, services):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    await services.catalog_sync._handle_table_deleted(
        acme_session, "mock", "prod", "orders", "acme"
    )

    tables = await services.catalog_sync.list_synced_tables(acme_session, "mock", "prod")
    assert "orders" not in {row["table_name"] for row in tables}

    detail = await services.catalog_sync.get_table_detail(
        acme_session, "mock", "prod", "orders"
    )
    assert detail is None


@pytest.mark.asyncio
async def test_lineage_is_recursive(acme_session, services):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")

    lineage = await services.catalog_sync.get_lineage(
        acme_session, "mock", "prod", "order_items"
    )

    upstream = {row["uri"]: row for row in lineage["upstream"]}
    assert "ctx://datalake/mock/prod/orders" in upstream
    assert "ctx://datalake/mock/prod/products" in upstream
    assert "ctx://datalake/mock/prod/users" in upstream
    assert upstream["ctx://datalake/mock/prod/users"]["depth"] == 2


@pytest.mark.asyncio
async def test_sql_context_filters_catalog_and_preserves_rank(
    acme_session, services, query_agent_ctx
):
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    await services.catalog_sync.sync_all(acme_session, "alt", "acme")

    class _StubRetrieval:
        async def search(self, db, request, ctx):
            return SearchResponse(
                results=[
                    SearchResult(
                        uri="ctx://datalake/alt/prod/orders",
                        context_type="table_schema",
                        scope="datalake",
                        score=0.99,
                        status="active",
                        version=1,
                    ),
                    SearchResult(
                        uri="ctx://datalake/mock/prod/users",
                        context_type="table_schema",
                        scope="datalake",
                        score=0.95,
                        status="active",
                        version=1,
                    ),
                    SearchResult(
                        uri="ctx://datalake/mock/prod/orders",
                        context_type="table_schema",
                        scope="datalake",
                        score=0.9,
                        status="active",
                        version=1,
                    ),
                ],
                total=3,
            )

    class _AllowAllACL:
        async def check_read_access(self, db, uri, ctx):
            return AccessDecision(allowed=True, field_masks=None, reason="ok")

    resp = await search_sql_context(
        SqlContextRequest(query="orders by user", catalog="mock", top_k=2),
        ctx=query_agent_ctx,
        db=acme_session,
        retrieval=_StubRetrieval(),
        acl=_AllowAllACL(),
        masking=MaskingService(),
    )

    assert [table.uri for table in resp.tables] == [
        "ctx://datalake/mock/prod/users",
        "ctx://datalake/mock/prod/orders",
    ]


def test_sidecar_bootstrap_repo_paths():
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / "bridge" / "src" / "sidecar.py"
    spec = importlib.util.spec_from_file_location("bridge_sidecar_test", module_path)
    assert spec is not None and spec.loader is not None
    sidecar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sidecar)

    sdk_src = str(repo_root / "sdk" / "src")
    plugin_src = str(repo_root / "plugins" / "openclaw" / "src")

    original_path = sys.path[:]
    try:
        sys.path[:] = [p for p in sys.path if p not in {sdk_src, plugin_src}]
        for name in ("openclaw.plugin", "openclaw", "contexthub_sdk"):
            sys.modules.pop(name, None)

        added = sidecar._bootstrap_repo_paths()

        assert sdk_src in sys.path
        assert plugin_src in sys.path
        assert set(added) == {sdk_src, plugin_src}

        importlib.invalidate_caches()
        assert importlib.import_module("contexthub_sdk")
        module = importlib.import_module("openclaw.plugin")
        assert hasattr(module, "ContextHubContextEngine")
    finally:
        sys.path[:] = original_path


def test_sidecar_main_wires_sdk_client_with_url_kwarg():
    repo_root = Path(__file__).resolve().parent.parent
    module_path = repo_root / "bridge" / "src" / "sidecar.py"
    spec = importlib.util.spec_from_file_location("bridge_sidecar_main_test", module_path)
    assert spec is not None and spec.loader is not None
    sidecar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sidecar)

    calls: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            calls["client_kwargs"] = kwargs

    class _FakeEngine:
        def __init__(self, client):
            calls["engine_client"] = client

    fake_sdk = types.ModuleType("contexthub_sdk")
    fake_sdk.ContextHubClient = _FakeClient
    fake_plugin = types.ModuleType("openclaw.plugin")
    fake_plugin.ContextHubContextEngine = _FakeEngine
    fake_uvicorn = types.ModuleType("uvicorn")

    def _run(app, host, port):
        calls["uvicorn_run"] = {"app": app, "host": host, "port": port}

    fake_uvicorn.run = _run

    original_modules = {
        name: sys.modules.get(name)
        for name in ("contexthub_sdk", "openclaw", "openclaw.plugin", "uvicorn")
    }
    try:
        sys.modules["contexthub_sdk"] = fake_sdk
        sys.modules["openclaw"] = types.ModuleType("openclaw")
        sys.modules["openclaw.plugin"] = fake_plugin
        sys.modules["uvicorn"] = fake_uvicorn

        sidecar.main(
            ["--port", "9100", "--contexthub-url", "http://localhost:8000"]
        )

        assert calls["client_kwargs"] == {
            "url": "http://localhost:8000",
            "api_key": "changeme",
            "agent_id": "sidecar-agent",
            "account_id": "acme",
        }
        assert calls["uvicorn_run"] == {
            "app": sidecar.app,
            "host": "0.0.0.0",
            "port": 9100,
        }
    finally:
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
