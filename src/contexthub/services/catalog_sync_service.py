"""CatalogSyncService: catalog → ContextHub metadata sync."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from contexthub.connectors.base import CatalogConnector
from contexthub.db.repository import ScopedRepo
from contexthub.generation.table_schema import TableSchemaGenerator
from contexthub.services.indexer_service import IndexerService

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    tables_synced: int = 0
    tables_created: int = 0
    tables_updated: int = 0
    tables_deleted: int = 0
    errors: list[str] = field(default_factory=list)


class CatalogSyncService:
    def __init__(
        self,
        connector: CatalogConnector,
        indexer: IndexerService,
        table_schema_generator: TableSchemaGenerator,
    ):
        self._connector = connector
        self._indexer = indexer
        self._generator = table_schema_generator

    async def sync_all(
        self, db: ScopedRepo, catalog: str, account_id: str
    ) -> SyncResult:
        """全量同步指定 catalog 的所有表。"""
        result = SyncResult()
        databases = await self._connector.list_databases()
        for database in databases:
            tables = await self._connector.list_tables(database)
            for table in tables:
                try:
                    ctx_id = await self.sync_table(db, catalog, database, table, account_id)
                    result.tables_synced += 1
                    # Check if it was new
                    row = await db.fetchrow(
                        "SELECT created_at, updated_at FROM contexts WHERE id = $1",
                        ctx_id,
                    )
                    if row and row["created_at"] == row["updated_at"]:
                        result.tables_created += 1
                    else:
                        result.tables_updated += 1
                except Exception as exc:
                    msg = f"{database}.{table}: {exc}"
                    logger.exception("sync_table failed: %s", msg)
                    result.errors.append(msg)

        # Sync relationships after all tables are in place
        await self._sync_relationships(db, catalog)
        return result

    async def sync_table(
        self,
        db: ScopedRepo,
        catalog: str,
        database: str,
        table: str,
        account_id: str,
    ) -> UUID:
        """同步单张表。返回 context_id。"""
        schema = await self._connector.get_table_schema(database, table)
        stats = await self._connector.get_table_stats(database, table)
        sample = await self._connector.get_sample_data(database, table)

        uri = f"ctx://datalake/{catalog}/{database}/{table}"

        # 1. Generate L0/L1
        generated = self._generator.generate_from_schema(schema)

        # 2. UPSERT contexts row
        row = await db.fetchrow(
            """
            INSERT INTO contexts (uri, context_type, scope, l0_content, l1_content, account_id)
            VALUES ($1, 'table_schema', 'datalake', $2, $3, $4)
            ON CONFLICT (account_id, uri) DO UPDATE SET
                l0_content = EXCLUDED.l0_content,
                l1_content = EXCLUDED.l1_content,
                status = 'active',
                stale_at = NULL,
                archived_at = NULL,
                updated_at = NOW()
            RETURNING id, (xmax = 0) AS is_new
            """,
            uri, generated.l0, generated.l1, account_id,
        )

        context_id = row["id"]
        is_new = row["is_new"]

        # 3. Check DDL change
        old_ddl = await db.fetchval(
            "SELECT ddl FROM table_metadata WHERE context_id = $1",
            context_id,
        )
        schema_changed = old_ddl is not None and old_ddl != schema.ddl

        # 4. UPSERT table_metadata
        await db.execute(
            """
            INSERT INTO table_metadata
                (context_id, catalog, database_name, table_name, ddl, stats, sample_data, stats_updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, NOW())
            ON CONFLICT (context_id) DO UPDATE SET
                ddl = EXCLUDED.ddl,
                stats = EXCLUDED.stats,
                sample_data = EXCLUDED.sample_data,
                stats_updated_at = NOW()
            """,
            context_id, catalog, database, table, schema.ddl,
            json.dumps({"row_count": stats.row_count, "size_bytes": stats.size_bytes}),
            json.dumps(sample),
        )

        # 5. If DDL changed or new table, insert change_events
        if schema_changed or is_new:
            change_type = "created" if is_new else "modified"
            diff_summary = f"schema {'created' if is_new else 'changed'}: {table}"
            await db.execute(
                """
                INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary)
                VALUES ($1, $2, $3, 'catalog_sync', $4)
                """,
                context_id, account_id, change_type, diff_summary,
            )
            await db.execute(
                "UPDATE contexts SET version = version + 1 WHERE id = $1",
                context_id,
            )

        # 6. Write embedding (best-effort)
        if generated.l0:
            await self._indexer.update_embedding(db, context_id, generated.l0)

        return context_id

    async def sync_changes(
        self, db: ScopedRepo, catalog: str, account_id: str, since: datetime
    ) -> SyncResult:
        """增量同步：检测并处理变更。"""
        result = SyncResult()
        changes = await self._connector.detect_changes(since)
        for change in changes:
            try:
                if change.change_type == "table_deleted":
                    await self._handle_table_deleted(
                        db, catalog, change.database, change.table, account_id
                    )
                    result.tables_deleted += 1
                else:
                    await self.sync_table(
                        db, catalog, change.database, change.table, account_id
                    )
                    if change.change_type == "table_created":
                        result.tables_created += 1
                    else:
                        result.tables_updated += 1
                result.tables_synced += 1
            except Exception as exc:
                msg = f"{change.database}.{change.table}: {exc}"
                logger.exception("sync_changes failed: %s", msg)
                result.errors.append(msg)
        return result

    async def _handle_table_deleted(
        self, db: ScopedRepo, catalog: str, database: str, table: str, account_id: str
    ) -> None:
        uri = f"ctx://datalake/{catalog}/{database}/{table}"
        row = await db.fetchrow(
            "SELECT id FROM contexts WHERE uri = $1 AND account_id = $2",
            uri, account_id,
        )
        if row is None:
            return
        context_id = row["id"]
        await db.execute(
            "UPDATE contexts SET status = 'archived', archived_at = NOW() WHERE id = $1",
            context_id,
        )
        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary)
            VALUES ($1, $2, 'deleted', 'catalog_sync', $3)
            """,
            context_id, account_id, f"table deleted: {table}",
        )

        # PropagationEngine skips 'deleted' events by design (cycle prevention),
        # so we directly mark dependents stale here.
        dependents = await db.fetch(
            """
            SELECT dependent_id
            FROM dependencies
            WHERE dependency_id = $1
              AND dep_type = 'table_schema'
            """,
            context_id,
        )
        for dep in dependents:
            result = await db.execute(
                """
                UPDATE contexts
                SET status = 'stale', stale_at = NOW(), updated_at = NOW()
                WHERE id = $1
                  AND status NOT IN ('stale', 'archived', 'deleted')
                """,
                dep["dependent_id"],
            )
            if result != "UPDATE 0":
                await db.execute(
                    """
                    INSERT INTO change_events
                        (context_id, account_id, change_type, actor, diff_summary)
                    VALUES ($1, $2, 'marked_stale', 'catalog_sync', $3)
                    """,
                    dep["dependent_id"], account_id,
                    f"dependency {table} was deleted",
                )

    async def _sync_relationships(self, db: ScopedRepo, catalog: str) -> None:
        """Sync FK relationships from connector into table_relationships."""
        rels = await self._connector.get_relationships()
        for rel in rels:
            uri_a = f"ctx://datalake/{catalog}/{rel.from_database}/{rel.from_table}"
            uri_b = f"ctx://datalake/{catalog}/{rel.to_database}/{rel.to_table}"
            id_a = await db.fetchval(
                "SELECT id FROM contexts WHERE uri = $1", uri_a
            )
            id_b = await db.fetchval(
                "SELECT id FROM contexts WHERE uri = $1", uri_b
            )
            if id_a is None or id_b is None:
                continue
            join_cols = json.dumps([{
                "from": rel.from_column,
                "to": rel.to_column,
            }])
            await db.execute(
                """
                INSERT INTO table_relationships (table_id_a, table_id_b, join_type, join_columns)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (table_id_a, table_id_b) DO UPDATE SET
                    join_type = EXCLUDED.join_type,
                    join_columns = EXCLUDED.join_columns
                """,
                id_a, id_b, rel.join_type, join_cols,
            )
            # Also write lineage
            await db.execute(
                """
                INSERT INTO lineage (upstream_id, downstream_id, transform_type, description)
                VALUES ($1, $2, 'fk', $3)
                ON CONFLICT (upstream_id, downstream_id) DO NOTHING
                """,
                id_b, id_a,
                f"{rel.from_table}.{rel.from_column} -> {rel.to_table}.{rel.to_column}",
            )

    async def list_synced_tables(
        self, db: ScopedRepo, catalog: str, database: str
    ) -> list[dict]:
        """List synced tables for a given catalog/database."""
        rows = await db.fetch(
            """
            SELECT c.uri, c.l0_content, c.status, c.version,
                   c.scope, c.owner_space,
                   tm.table_name, tm.ddl, tm.stats, tm.stats_updated_at
            FROM contexts c
            JOIN table_metadata tm ON tm.context_id = c.id
            WHERE tm.catalog = $1 AND tm.database_name = $2
              AND c.context_type = 'table_schema'
              AND c.status NOT IN ('archived', 'deleted')
            ORDER BY tm.table_name
            """,
            catalog, database,
        )
        return [dict(r) for r in rows]

    async def get_table_detail(
        self, db: ScopedRepo, catalog: str, database: str, table: str
    ) -> dict | None:
        """Get full table context including metadata, relationships, templates."""
        row = await db.fetchrow(
            """
            SELECT c.id, c.uri, c.l0_content, c.l1_content, c.l2_content,
                   c.status, c.version,
                   tm.ddl, tm.partition_info, tm.stats, tm.sample_data, tm.stats_updated_at
            FROM contexts c
            JOIN table_metadata tm ON tm.context_id = c.id
            WHERE tm.catalog = $1 AND tm.database_name = $2 AND tm.table_name = $3
              AND c.context_type = 'table_schema'
              AND c.status NOT IN ('archived', 'deleted')
            """,
            catalog, database, table,
        )
        if row is None:
            return None
        result = dict(row)
        # Fetch relationships
        rels = await db.fetch(
            """
            SELECT tr.join_type, tr.join_columns, tr.confidence,
                   CASE WHEN tr.table_id_a = $1 THEN c2.uri ELSE c3.uri END AS related_table
            FROM table_relationships tr
            LEFT JOIN contexts c2 ON c2.id = tr.table_id_b
            LEFT JOIN contexts c3 ON c3.id = tr.table_id_a
            WHERE tr.table_id_a = $1 OR tr.table_id_b = $1
            """,
            row["id"],
        )
        result["relationships"] = [dict(r) for r in rels]
        # Fetch templates
        templates = await db.fetch(
            """
            SELECT sql_template, description, hit_count
            FROM query_templates
            WHERE context_id = $1
            ORDER BY hit_count DESC
            LIMIT 5
            """,
            row["id"],
        )
        result["templates"] = [dict(r) for r in templates]
        return result

    async def get_lineage(
        self, db: ScopedRepo, catalog: str, database: str, table: str
    ) -> dict:
        """Get lineage (upstream + downstream) for a table."""
        ctx = await db.fetchrow(
            """
            SELECT c.id FROM contexts c
            JOIN table_metadata tm ON tm.context_id = c.id
            WHERE tm.catalog = $1 AND tm.database_name = $2 AND tm.table_name = $3
              AND c.status NOT IN ('archived', 'deleted')
            """,
            catalog, database, table,
        )
        if ctx is None:
            return {"upstream": [], "downstream": []}
        context_id = ctx["id"]
        upstream = await db.fetch(
            """
            WITH RECURSIVE upstream_lineage AS (
                SELECT
                    l.upstream_id,
                    l.downstream_id,
                    l.transform_type,
                    l.description,
                    1 AS depth,
                    ARRAY[l.downstream_id, l.upstream_id]::uuid[] AS path
                FROM lineage l
                WHERE l.downstream_id = $1

                UNION ALL

                SELECT
                    l.upstream_id,
                    l.downstream_id,
                    l.transform_type,
                    l.description,
                    ul.depth + 1,
                    ul.path || l.upstream_id
                FROM lineage l
                JOIN upstream_lineage ul ON l.downstream_id = ul.upstream_id
                WHERE NOT l.upstream_id = ANY(ul.path)
            )
            SELECT DISTINCT ON (c.uri)
                c.uri,
                ul.transform_type,
                ul.description,
                ul.depth
            FROM upstream_lineage ul
            JOIN contexts c ON c.id = ul.upstream_id
            WHERE c.status NOT IN ('archived', 'deleted')
            ORDER BY c.uri, ul.depth ASC
            """,
            context_id,
        )
        downstream = await db.fetch(
            """
            WITH RECURSIVE downstream_lineage AS (
                SELECT
                    l.upstream_id,
                    l.downstream_id,
                    l.transform_type,
                    l.description,
                    1 AS depth,
                    ARRAY[l.upstream_id, l.downstream_id]::uuid[] AS path
                FROM lineage l
                WHERE l.upstream_id = $1

                UNION ALL

                SELECT
                    l.upstream_id,
                    l.downstream_id,
                    l.transform_type,
                    l.description,
                    dl.depth + 1,
                    dl.path || l.downstream_id
                FROM lineage l
                JOIN downstream_lineage dl ON l.upstream_id = dl.downstream_id
                WHERE NOT l.downstream_id = ANY(dl.path)
            )
            SELECT DISTINCT ON (c.uri)
                c.uri,
                dl.transform_type,
                dl.description,
                dl.depth
            FROM downstream_lineage dl
            JOIN contexts c ON c.id = dl.downstream_id
            WHERE c.status NOT IN ('archived', 'deleted')
            ORDER BY c.uri, dl.depth ASC
            """,
            context_id,
        )
        return {
            "upstream": [dict(r) for r in upstream],
            "downstream": [dict(r) for r in downstream],
        }
