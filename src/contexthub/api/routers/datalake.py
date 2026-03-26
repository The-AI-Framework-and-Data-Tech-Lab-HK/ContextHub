"""Datalake API router: catalog sync, table detail, lineage, sql-context."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from contexthub.api.deps import get_db, get_request_context, get_retrieval_service
from contexthub.db.repository import ScopedRepo
from contexthub.models.context import ContextType, Scope
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest
from contexthub.services.catalog_sync_service import CatalogSyncService
from contexthub.services.retrieval_service import RetrievalService

router = APIRouter(prefix="/api/v1", tags=["datalake"])


def _get_catalog_sync_service(request: Request) -> CatalogSyncService:
    return request.app.state.catalog_sync_service


# --- Request / Response models ---

class SyncRequest(BaseModel):
    catalog: str = "mock"

class SyncResponse(BaseModel):
    tables_synced: int
    tables_created: int
    tables_updated: int
    tables_deleted: int
    errors: list[str]


class SqlContextRequest(BaseModel):
    query: str
    catalog: str = "mock"
    top_k: int = Field(default=5, ge=1, le=20)
    include_templates: bool = True
    include_relationships: bool = True
    include_sample_data: bool = False


class SqlContextTableInfo(BaseModel):
    uri: str
    l0_content: str | None = None
    l1_content: str | None = None
    ddl: str | None = None
    partition_info: dict | None = None
    sample_data: list[dict] | None = None
    joins: list[dict] | None = None
    top_templates: list[dict] | None = None


class SqlContextResponse(BaseModel):
    tables: list[SqlContextTableInfo]
    total_tables_found: int


# --- Endpoints ---

@router.post("/datalake/sync", response_model=SyncResponse)
async def sync_all(
    body: SyncRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: CatalogSyncService = Depends(_get_catalog_sync_service),
):
    result = await svc.sync_all(db, body.catalog, ctx.account_id)
    return SyncResponse(
        tables_synced=result.tables_synced,
        tables_created=result.tables_created,
        tables_updated=result.tables_updated,
        tables_deleted=result.tables_deleted,
        errors=result.errors,
    )


@router.post("/datalake/sync/{catalog}/{database}/{table}", response_model=dict)
async def sync_table(
    catalog: str,
    database: str,
    table: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: CatalogSyncService = Depends(_get_catalog_sync_service),
):
    context_id = await svc.sync_table(db, catalog, database, table, ctx.account_id)
    return {"context_id": str(context_id)}


@router.get("/datalake/{catalog}/{database}")
async def list_tables(
    catalog: str,
    database: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: CatalogSyncService = Depends(_get_catalog_sync_service),
):
    tables = await svc.list_synced_tables(db, catalog, database)
    return {"tables": tables}


@router.get("/datalake/{catalog}/{database}/{table}")
async def get_table_detail(
    catalog: str,
    database: str,
    table: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: CatalogSyncService = Depends(_get_catalog_sync_service),
):
    detail = await svc.get_table_detail(db, catalog, database, table)
    if detail is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Table not found")
    return detail


@router.get("/datalake/{catalog}/{database}/{table}/lineage")
async def get_lineage(
    catalog: str,
    database: str,
    table: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: CatalogSyncService = Depends(_get_catalog_sync_service),
):
    return await svc.get_lineage(db, catalog, database, table)


@router.post("/search/sql-context", response_model=SqlContextResponse)
async def search_sql_context(
    body: SqlContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    retrieval: RetrievalService = Depends(get_retrieval_service),
):
    """SQL 生成上下文组装：复用 RetrievalService 做候选检索，再拉取结构化信息。"""
    search_req = SearchRequest(
        query=body.query,
        scope=[Scope.DATALAKE],
        context_type=[ContextType.TABLE_SCHEMA],
        # Reuse canonical retrieval, but over-fetch here so catalog filtering
        # below still has a reasonable chance to return top_k matches.
        top_k=min(body.top_k * 5, 100),
    )
    search_resp = await retrieval.search(db, search_req, ctx)

    if not search_resp.results:
        return SqlContextResponse(tables=[], total_tables_found=0)

    context_ids = []
    seen_ids: set = set()
    catalog_prefix = f"ctx://datalake/{body.catalog}/"
    for r in search_resp.results:
        if not r.uri.startswith(catalog_prefix):
            continue
        row = await db.fetchrow(
            """
            SELECT c.id
            FROM contexts c
            JOIN table_metadata tm ON tm.context_id = c.id
            WHERE c.uri = $1
              AND tm.catalog = $2
              AND c.status NOT IN ('archived', 'deleted')
            """,
            r.uri,
            body.catalog,
        )
        if row and row["id"] not in seen_ids:
            context_ids.append(row["id"])
            seen_ids.add(row["id"])
        if len(context_ids) >= body.top_k:
            break

    if not context_ids:
        return SqlContextResponse(tables=[], total_tables_found=0)

    # Batch fetch structured info
    rows = await db.fetch(
        """
        SELECT
            c.id, c.uri, c.l0_content, c.l1_content,
            tm.ddl, tm.partition_info, tm.sample_data,
            (SELECT jsonb_agg(jsonb_build_object(
                'related_table', CASE WHEN tr.table_id_a = c.id THEN c2.uri ELSE c3.uri END,
                'join_columns', tr.join_columns))
             FROM table_relationships tr
             LEFT JOIN contexts c2 ON c2.id = tr.table_id_b
             LEFT JOIN contexts c3 ON c3.id = tr.table_id_a
             WHERE tr.table_id_a = c.id OR tr.table_id_b = c.id
            ) AS joins,
            (SELECT jsonb_agg(jsonb_build_object('sql', qt.sql_template, 'description', qt.description))
             FROM (SELECT * FROM query_templates WHERE context_id = c.id ORDER BY hit_count DESC LIMIT 5) qt
            ) AS top_templates
        FROM contexts c
        JOIN table_metadata tm ON tm.context_id = c.id
        WHERE c.id = ANY($1::uuid[])
          AND tm.catalog = $2
        ORDER BY array_position($1::uuid[], c.id)
        """,
        context_ids,
        body.catalog,
    )

    tables = []
    for row in rows:
        info = SqlContextTableInfo(
            uri=row["uri"],
            l0_content=row["l0_content"],
            l1_content=row["l1_content"],
            ddl=row["ddl"],
            partition_info=row["partition_info"],
        )
        if body.include_sample_data:
            info.sample_data = row["sample_data"]
        if body.include_relationships and row["joins"]:
            info.joins = row["joins"]
        if body.include_templates and row["top_templates"]:
            info.top_templates = row["top_templates"]
        tables.append(info)

    return SqlContextResponse(tables=tables, total_tables_found=len(tables))
