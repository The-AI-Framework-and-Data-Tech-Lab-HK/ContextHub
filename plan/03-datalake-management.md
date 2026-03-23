# 03 — 数据湖表管理

## 设计决策：L2 拆解为结构化表

通用上下文（技能、记忆）的 L2 是一整块文本，存 `contexts.l2_content` 即可。但数据湖表的 L2 包含多种**更新频率不同**的结构化数据：

| 组成部分 | 变更频率 | 是否触发下游传播 |
|----------|----------|-----------------|
| DDL（表结构定义） | 低（ALTER TABLE） | 是 — schema 变更影响查询模板和 Skill |
| 分区信息 | 中（新分区写入） | 否 — 不影响查询逻辑 |
| 统计信息（行数、大小） | 高（每次 catalog sync） | 否 — 不影响查询逻辑 |
| 数据血缘 | 低（ETL 管道变更） | 视情况 |
| 查询模板 | 中（Agent 积累新模板） | 否 — 模板是下游，不是上游 |

如果全塞进一个 TEXT blob，每次统计信息更新都要重写整个 L2，且无法区分"什么变了"来决定是否传播。因此数据湖表的 L2 拆解为独立的 PG 表。

## (a) 湖表元数据的 PG 表结构

```sql
-- 数据湖表的结构化元数据（contexts 表的扩展）
CREATE TABLE table_metadata (
    context_uri     TEXT PRIMARY KEY REFERENCES contexts(uri),
    catalog         TEXT NOT NULL,          -- 'hive' | 'iceberg' | 'delta'
    database_name   TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    ddl             TEXT,                   -- 完整 DDL
    partition_info  JSONB,                  -- 分区字段和策略
    stats           JSONB,                  -- {"row_count": 1000000, "size_bytes": ..., "last_updated": ...}
    sample_data     JSONB,                  -- 样例数据行
    stats_updated_at TIMESTAMPTZ            -- 统计信息单独追踪更新时间
);

-- 数据血缘（有向图）
CREATE TABLE lineage (
    source_uri      TEXT NOT NULL,          -- 上游表
    target_uri      TEXT NOT NULL,          -- 下游表
    transform_type  TEXT,                   -- 'etl' | 'view' | 'derived'
    description     TEXT,
    PRIMARY KEY (source_uri, target_uri)
);

-- 表间 JOIN 关系
CREATE TABLE table_relationships (
    table_uri_a     TEXT NOT NULL,
    table_uri_b     TEXT NOT NULL,
    join_type       TEXT,                   -- 'fk' | 'common_join' | 'inferred'
    join_columns    JSONB NOT NULL,         -- [{"a": "user_id", "b": "id"}]
    confidence      FLOAT DEFAULT 1.0,     -- 推断关系的置信度
    PRIMARY KEY (table_uri_a, table_uri_b)
);

-- 查询模板
CREATE TABLE query_templates (
    id              SERIAL PRIMARY KEY,
    context_uri     TEXT NOT NULL REFERENCES contexts(uri),
    sql_template    TEXT NOT NULL,
    description     TEXT,
    hit_count       INT DEFAULT 0,
    last_used_at    TIMESTAMPTZ,
    created_by      TEXT                    -- agent_id
);

CREATE INDEX idx_qt_context ON query_templates (context_uri);
```

### 湖表 Context 示例

```
ctx://datalake/hive/prod/orders

contexts 表:
  uri:          ctx://datalake/hive/prod/orders
  context_type: table_schema
  scope:        datalake
  l0_content:   "orders 表 - 存储所有订单交易记录，包含订单金额、状态、时间等"
  l1_content:   "## Schema\n| 字段 | 类型 | 说明 |\n| order_id | BIGINT | 订单ID，主键 |..."
  l2_content:   NULL  ← 数据湖表不用此列，改用结构化子表

table_metadata 表:
  context_uri:  ctx://datalake/hive/prod/orders
  catalog:      hive
  database_name: prod
  table_name:   orders
  ddl:          "CREATE TABLE orders (order_id BIGINT, user_id BIGINT, ...)"
  partition_info: {"keys": ["created_at"], "type": "range", "granularity": "day"}
  stats:        {"row_count": 5000000, "size_bytes": 2147483648, "freshness": "2026-03-18"}
  sample_data:  [{"order_id": 1001, "user_id": 42, "amount": 299.00, "status": "completed"}]

table_relationships 表:
  table_uri_a:  ctx://datalake/hive/prod/orders
  table_uri_b:  ctx://datalake/hive/prod/users
  join_type:    fk
  join_columns: [{"a": "user_id", "b": "id"}]

lineage 表:
  source_uri:   ctx://datalake/hive/ods/ods_orders
  target_uri:   ctx://datalake/hive/prod/orders
  transform_type: etl
  description:  "ODS 层清洗后写入 prod"
```

### 精确的变更传播

```sql
-- 统计信息更新：不触发下游传播（行数变了不代表 schema 变了）
UPDATE table_metadata SET stats = $1, stats_updated_at = NOW()
WHERE context_uri = $2;
-- 不插入 change_event，不通知任何人

-- schema 变更：触发传播
BEGIN;
  UPDATE table_metadata SET ddl = $1 WHERE context_uri = $2;
  UPDATE contexts SET version = version + 1, updated_at = NOW() WHERE uri = $2;
  INSERT INTO change_events (source_uri, change_type, actor, diff_summary)
    VALUES ($2, 'modified', 'catalog_sync', 'schema 变更: 新增字段 discount_rate DECIMAL');
COMMIT;
-- PG NOTIFY → 传播引擎标记依赖此表的 Skill/cases 为 stale
```

## (b) 通用 CatalogConnector 接口

```python
class CatalogConnector(ABC):
    """通用数据目录连接器，后续可实现 Hive/Iceberg/Delta Lake 等"""
    async def list_databases(self) -> list[str]
    async def list_tables(self, database: str) -> list[str]
    async def get_table_schema(self, database: str, table: str) -> TableSchema
    async def get_table_stats(self, database: str, table: str) -> TableStats
    async def get_sample_data(self, database: str, table: str, limit: int) -> list[dict]
    async def detect_changes(self, since: datetime) -> list[CatalogChange]
```

CatalogConnector 拉取的数据写入 PG 的流程：

```python
async def sync_table(self, catalog: str, db: str, table: str):
    schema = await self.connector.get_table_schema(db, table)
    stats = await self.connector.get_table_stats(db, table)
    uri = f"ctx://datalake/{catalog}/{db}/{table}"

    async with self.pg.transaction():
        # 1. 更新或创建 contexts 行（L0/L1 由 LLM 生成）
        await self.pg.execute("""
            INSERT INTO contexts (uri, context_type, scope, l0_content, l1_content, account_id)
            VALUES ($1, 'table_schema', 'datalake', $2, $3, $4)
            ON CONFLICT (uri) DO UPDATE SET l1_content = $3, updated_at = NOW()
        """, uri, generate_l0(schema), generate_l1(schema), account_id)

        # 2. 更新 table_metadata
        await self.pg.execute("""
            INSERT INTO table_metadata (context_uri, catalog, database_name, table_name, ddl, stats)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (context_uri) DO UPDATE SET ddl = $5, stats = $6, stats_updated_at = NOW()
        """, uri, catalog, db, table, schema.ddl, stats.to_json())

        # 3. 如果 DDL 变了，插入变更事件
        if schema_changed:
            await self.pg.execute(
                "INSERT INTO change_events (source_uri, change_type, actor) VALUES ($1, 'modified', 'catalog_sync')",
                uri)
```

## (c) Text-to-SQL 上下文组装

根据用户问题，组装完整的 SQL 生成上下文。利用 PG JOIN 一次查询完成：

```sql
-- 输入：pgvector 检索返回的相关表 URI 列表 $relevant_uris
SELECT
    c.uri, c.l0_content, c.l1_content,
    tm.ddl, tm.partition_info, tm.sample_data,
    -- 聚合该表的 JOIN 关系
    (SELECT jsonb_agg(jsonb_build_object(
        'related_table', CASE WHEN tr.table_uri_a = c.uri THEN tr.table_uri_b ELSE tr.table_uri_a END,
        'join_columns', tr.join_columns))
     FROM table_relationships tr
     WHERE tr.table_uri_a = c.uri OR tr.table_uri_b = c.uri
    ) AS joins,
    -- 聚合该表的查询模板（按使用频率排序，取 top 5）
    (SELECT jsonb_agg(jsonb_build_object('sql', qt.sql_template, 'description', qt.description))
     FROM (SELECT * FROM query_templates WHERE context_uri = c.uri ORDER BY hit_count DESC LIMIT 5) qt
    ) AS top_templates
FROM contexts c
JOIN table_metadata tm ON tm.context_uri = c.uri
WHERE c.uri = ANY($relevant_uris);
```

完整组装流程：
1. 向量检索相关表的 L0 → top-K URI
2. 上述 SQL 一次性拉取：schema + JOIN 关系 + 查询模板
3. 附加历史成功查询的 cases（从 `contexts` 表查 `context_type='memory'` 且 `scope='agent'`）
4. 附加业务术语表（从 `contexts` 表查 `uri LIKE 'ctx://team/memories/data_dictionary/%'`）

### 血缘查询（多跳）

```sql
-- 查找 orders 表的所有上游数据源
WITH RECURSIVE upstream AS (
    SELECT source_uri, target_uri, 1 AS depth
    FROM lineage WHERE target_uri = 'ctx://datalake/hive/prod/orders'
    UNION ALL
    SELECT l.source_uri, l.target_uri, u.depth + 1
    FROM lineage l JOIN upstream u ON l.target_uri = u.source_uri
    WHERE u.depth < 5  -- 最多追溯 5 跳
)
SELECT * FROM upstream;
```
