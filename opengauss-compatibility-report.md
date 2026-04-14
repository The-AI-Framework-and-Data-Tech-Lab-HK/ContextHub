# ContextHub: PostgreSQL 16 → openGauss 7.0.0 SQL 兼容性分析报告

> 分析时间: 2026-04-13
>
> 参考文档:
> - https://github.com/opengauss-mirror
> - https://docs.opengauss.org/zh/docs/7.0.0-RC1

---

## 一、项目概述

ContextHub 是一个基于 **FastAPI + asyncpg + PostgreSQL 16** 的上下文管理 REST API。项目使用：

- **asyncpg** 连接池执行原生参数化 SQL（无 ORM）
- **Alembic** 管理 DDL 迁移（通过 `op.execute()` 执行原生 SQL）
- **pgvector** 扩展做向量相似度检索
- **pgcrypto** 扩展提供 `gen_random_uuid()`
- **Row Level Security (RLS)** 实现多租户隔离
- **LISTEN/NOTIFY** (`pg_notify`) 实现事件传播唤醒

---

## 二、SQL 语句全量清单

以下按文件分类罗列仓库中所有实际执行的 SQL 语句。

### 2.1 Alembic 迁移（DDL 层）

#### `alembic/versions/001_initial_schema.py`

| # | SQL 语句摘要 | 类型 |
|---|---|---|
| 1 | `CREATE EXTENSION IF NOT EXISTS vector` | DDL |
| 2 | `CREATE EXTENSION IF NOT EXISTS pgcrypto` | DDL |
| 3 | `CREATE TABLE contexts (... UUID PRIMARY KEY DEFAULT gen_random_uuid(), ... vector(1536), TEXT[], TIMESTAMPTZ, JSONB ...)` | DDL |
| 4 | `ALTER TABLE contexts ENABLE ROW LEVEL SECURITY` | DDL |
| 5 | `ALTER TABLE contexts FORCE ROW LEVEL SECURITY` | DDL |
| 6 | `CREATE POLICY tenant_isolation ON contexts USING (account_id = current_setting('app.account_id'))` | DDL |
| 7 | `CREATE INDEX idx_contexts_scope ON contexts (scope, context_type)` | DDL |
| 8 | `CREATE INDEX idx_contexts_owner ON contexts (account_id, owner_space)` | DDL |
| 9 | `CREATE INDEX idx_contexts_status ON contexts (status) WHERE status != 'deleted'` | DDL（部分索引） |
| 10 | `CREATE INDEX idx_contexts_l0_embedding ON contexts USING hnsw (l0_embedding vector_cosine_ops) WITH (m=16, ef_construction=64)` | DDL（HNSW 向量索引） |
| 11 | `CREATE TABLE dependencies (... SERIAL PRIMARY KEY ...)` | DDL |
| 12 | `CREATE TABLE change_events (... UUID PRIMARY KEY DEFAULT gen_random_uuid(), JSONB, TIMESTAMPTZ ...)` | DDL |
| 13 | `CREATE INDEX ... WHERE delivery_status IN ('pending', 'retry')` | DDL（部分索引） |
| 14 | `CREATE INDEX ... WHERE delivery_status = 'processing'` | DDL（部分索引） |
| 15 | `CREATE OR REPLACE FUNCTION notify_change_event() RETURNS trigger AS $$ ... pg_notify ... $$ LANGUAGE plpgsql` | DDL（触发器函数） |
| 16 | `CREATE TRIGGER trg_change_events_notify AFTER INSERT ON change_events FOR EACH ROW EXECUTE FUNCTION notify_change_event()` | DDL（触发器） |
| 17 | `CREATE TABLE teams (... UUID PRIMARY KEY DEFAULT gen_random_uuid() ...)` | DDL |
| 18 | `CREATE POLICY tenant_isolation ON teams USING (...)` | DDL |
| 19 | `CREATE TABLE team_memberships (... PRIMARY KEY (agent_id, team_id) ...)` | DDL |
| 20 | `CREATE TABLE skill_versions (... PRIMARY KEY (skill_id, version) ...)` | DDL |
| 21 | `CREATE TABLE skill_subscriptions (... SERIAL PRIMARY KEY ...)` | DDL |
| 22 | `CREATE POLICY tenant_isolation ON skill_subscriptions USING (...)` | DDL |
| 23 | `CREATE TABLE table_metadata (... UUID PRIMARY KEY REFERENCES ..., JSONB ...)` | DDL |
| 24 | `CREATE TABLE lineage (... PRIMARY KEY (upstream_id, downstream_id) ...)` | DDL |
| 25 | `CREATE TABLE table_relationships (... JSONB NOT NULL, FLOAT ...)` | DDL |
| 26 | `CREATE TABLE query_templates (... SERIAL PRIMARY KEY ...)` | DDL |
| 27 | `INSERT INTO teams VALUES (UUID literal, ...)` — 种子数据 | DML |
| 28 | `INSERT INTO team_memberships VALUES (...)` — 种子数据 | DML |
| 29 | `DROP TABLE IF EXISTS ... CASCADE` — 降级 | DDL |
| 30 | `DROP FUNCTION IF EXISTS notify_change_event() CASCADE` — 降级 | DDL |

#### `alembic/versions/002_force_row_level_security.py`

| # | SQL 语句摘要 | 类型 |
|---|---|---|
| 31 | `ALTER TABLE contexts/teams/skill_subscriptions FORCE ROW LEVEL SECURITY` | DDL |
| 32 | `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` — 降级 | DDL |

### 2.2 数据库连接层

#### `src/contexthub/db/repository.py`

| # | SQL 语句 | 用途 |
|---|---|---|
| 33 | `SELECT set_config('app.account_id', $1, true)` | 设置事务级 GUC，实现 RLS 租户绑定 |

### 2.3 应用服务层（DML）

#### `src/contexthub/services/acl_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 34 | `SELECT scope, owner_space FROM contexts WHERE uri = $1 AND status != 'deleted'` | ACL 读/写检查 |
| 35 | `WITH RECURSIVE visible_teams AS (... UNION ALL ...) SELECT DISTINCT path FROM visible_teams` | 递归 CTE 获取可见团队 |
| 36 | `SELECT 1 FROM team_memberships tm JOIN teams t ON ... WHERE tm.agent_id = $1 AND t.path = $2 AND tm.access = 'read_write'` | 团队写权限检查 |

#### `src/contexthub/store/context_store.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 37 | `SELECT {col} FROM contexts WHERE uri = $1 AND status != 'deleted'` | 读取上下文内容 |
| 38 | `UPDATE contexts SET last_accessed_at = NOW() WHERE uri = $1` | 更新访问时间 |
| 39 | `UPDATE contexts SET {col} = $1, ... version = version + 1 ... WHERE uri = $2 AND version = $3 ... RETURNING id, version` | 乐观锁写入 |
| 40 | `SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'` | 存在性检查 |
| 41 | `INSERT INTO change_events (context_id, account_id, change_type, actor) VALUES ($1, current_setting('app.account_id'), 'modified', $2)` | 变更事件记录 |
| 42 | `SELECT uri, scope, owner_space, status FROM contexts WHERE uri LIKE $1 AND status != 'deleted'` | 目录列表 |
| 43 | `SELECT id, uri, context_type, ... FROM contexts WHERE uri = $1 AND status != 'deleted'` | stat 查询 |

#### `src/contexthub/services/context_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 44 | `INSERT INTO contexts (...) VALUES ($1,...,$9) RETURNING *` | 创建上下文 |
| 45 | `INSERT INTO change_events ... VALUES ($1, current_setting('app.account_id'), 'created', $2)` | 变更事件 |
| 46 | `UPDATE contexts SET {动态set子句} WHERE uri = $n AND version = $m AND status != 'deleted' RETURNING *` | 更新上下文 |
| 47 | `UPDATE contexts SET status = 'deleted', deleted_at = NOW(), ... WHERE uri = $1 AND version = $2 ... RETURNING id` | 软删除 |
| 48 | `SELECT id FROM contexts WHERE uri = $1 AND status != 'deleted'` | 依赖查询 |
| 49 | `SELECT d.dep_type, d.pinned_version, c1.uri, c2.uri FROM dependencies d JOIN contexts c1 ... JOIN contexts c2 ... WHERE d.dependent_id = $1 OR d.dependency_id = $1` | 依赖图查询 |

#### `src/contexthub/services/memory_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 50 | `INSERT INTO contexts (...) VALUES ($1, 'memory', 'agent', ..., current_setting('app.account_id'), ...) RETURNING *` | 添加记忆 |
| 51 | `SELECT uri, l0_content, ... FROM contexts WHERE context_type = 'memory' AND scope IN ('agent','team') ... ORDER BY updated_at DESC` | 列出记忆 |
| 52 | `SELECT * FROM contexts WHERE uri = $1 AND status != 'deleted'` | 读取源记忆 |
| 53 | `INSERT INTO contexts (...) VALUES ($1, 'memory', 'team', ...) RETURNING *` | 促进记忆 |
| 54 | `INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'derived_from')` | 依赖关系 |
| 55 | `INSERT INTO change_events (..., metadata) VALUES ($1, ..., $3)` | 变更事件（含元数据） |

#### `src/contexthub/services/skill_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 56 | `SELECT id, context_type FROM contexts WHERE uri = $1 AND status != 'deleted'` | 技能检查 |
| 57 | `SELECT id FROM contexts WHERE id = $1 FOR UPDATE` | 行锁防并发发布 |
| 58 | `SELECT COALESCE(MAX(version), 0) FROM skill_versions WHERE skill_id = $1` | 获取最新版本号 |
| 59 | `INSERT INTO skill_versions (...) VALUES ($1, $2, $3, $4, $5, 'published', $6, NOW())` | 插入技能版本 |
| 60 | `UPDATE contexts SET l0_content=$1, l1_content=$2, l2_content=$3, version=$4 ... WHERE id=$5` | 更新技能头指针 |
| 61 | `INSERT INTO change_events (..., new_version, metadata) VALUES (...)` | 版本发布事件 |
| 62 | `SELECT ... FROM skill_versions WHERE skill_id=$1 AND status IN ('published','deprecated') ORDER BY version DESC` | 获取版本列表 |
| 63 | `INSERT INTO skill_subscriptions ... ON CONFLICT (agent_id, skill_id) DO UPDATE SET pinned_version = EXCLUDED.pinned_version RETURNING *` | **UPSERT 订阅** |
| 64 | `SELECT pinned_version FROM skill_subscriptions WHERE agent_id=$1 AND skill_id=$2` | 查询订阅 |
| 65 | `SELECT MAX(version) FROM skill_versions WHERE skill_id=$1 AND status='published'` | 最新发布版本 |
| 66 | `SELECT content, version, status FROM skill_versions WHERE ... AND status IN ('published','deprecated')` | 读取版本 |
| 67 | `SELECT l2_content, version FROM contexts WHERE id = $1` | 读取技能内容 |
| 68 | `SELECT 1 FROM skill_versions WHERE skill_id=$1 AND status='published' LIMIT 1` | 发布版本存在性 |

#### `src/contexthub/services/retrieval_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 69 | `SELECT id, l2_content FROM contexts WHERE id IN ($1,$2,...)` | L2 按需加载 |
| 70 | `UPDATE contexts SET active_count = active_count + 1, last_accessed_at = NOW() WHERE id = ANY($1)` | 更新活跃计数 |

#### `src/contexthub/retrieval/vector_strategy.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 71 | `SELECT ... 1 - (l0_embedding <=> $1::vector) AS cosine_similarity FROM contexts WHERE ... ORDER BY l0_embedding <=> $1::vector LIMIT $n` | **pgvector 余弦相似度检索** |

#### `src/contexthub/retrieval/keyword_strategy.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 72 | `SELECT ... (CASE WHEN LOWER(COALESCE(...)) LIKE $n THEN 1 ELSE 0 END + ...)::float / {max} AS cosine_similarity FROM contexts WHERE ... ORDER BY ... DESC LIMIT $n` | 关键词回退检索 |

#### `src/contexthub/services/indexer_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 73 | `UPDATE contexts SET l0_embedding = NULL WHERE id = $1` | 清除向量嵌入 |
| 74 | `SELECT id, l0_content FROM contexts WHERE l0_embedding IS NULL AND l0_content IS NOT NULL AND status IN ('active','stale') LIMIT $1` | 回填选择 |
| 75 | `UPDATE contexts SET l0_embedding = $1::vector WHERE id = $2` | 写入向量嵌入 |

#### `src/contexthub/services/catalog_sync_service.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 76 | `SELECT created_at, updated_at FROM contexts WHERE id = $1` | 判断新建/更新 |
| 77 | `INSERT INTO contexts (...) VALUES (...) ON CONFLICT (account_id, uri) DO UPDATE SET ... RETURNING id, (xmax = 0) AS is_new` | **UPSERT + xmax 判断** |
| 78 | `SELECT ddl FROM table_metadata WHERE context_id = $1` | DDL 变更检测 |
| 79 | `INSERT INTO table_metadata (...) VALUES ($1,...,$6::jsonb,$7::jsonb, NOW()) ON CONFLICT (context_id) DO UPDATE SET ...` | **UPSERT 表元数据** |
| 80 | `INSERT INTO change_events (...) VALUES (...)` | 变更事件 |
| 81 | `UPDATE contexts SET version = version + 1 WHERE id = $1` | 版本递增 |
| 82 | `SELECT id FROM contexts WHERE uri = $1 AND account_id = $2` | 定位上下文 |
| 83 | `UPDATE contexts SET status = 'archived', archived_at = NOW() WHERE id = $1` | 归档 |
| 84 | `SELECT dependent_id FROM dependencies WHERE dependency_id = $1 AND dep_type = 'table_schema'` | 查询依赖者 |
| 85 | `UPDATE contexts SET status = 'stale', stale_at = NOW(), updated_at = NOW() WHERE id = $1 AND status NOT IN (...)` | 标记过期 |
| 86 | `INSERT INTO table_relationships (...) VALUES ($1,$2,$3,$4::jsonb) ON CONFLICT ... DO UPDATE SET ...` | **UPSERT 表关系** |
| 87 | `INSERT INTO lineage (...) VALUES ($1,$2,'fk',$3) ON CONFLICT ... DO NOTHING` | **UPSERT 血缘（DO NOTHING）** |
| 88 | `SELECT c.uri, ... FROM contexts c JOIN table_metadata tm ON ... WHERE ... ORDER BY tm.table_name` | 列出同步表 |
| 89 | `SELECT c.id, c.uri, ... FROM contexts c JOIN table_metadata tm ON ... WHERE ...` | 表详情 |
| 90 | `SELECT tr.join_type, tr.join_columns, ... CASE WHEN ... FROM table_relationships tr LEFT JOIN contexts ...` | 关系查询 |
| 91 | `SELECT sql_template, description, hit_count FROM query_templates WHERE context_id=$1 ORDER BY hit_count DESC LIMIT 5` | 查询模板 |
| 92 | `WITH RECURSIVE upstream_lineage AS (... ARRAY[...]::uuid[] ... NOT l.upstream_id = ANY(ul.path) ...) SELECT DISTINCT ON (c.uri) ... ORDER BY c.uri, ul.depth ASC` | **递归 CTE 血缘上游** |
| 93 | `WITH RECURSIVE downstream_lineage AS (...) SELECT DISTINCT ON (c.uri) ...` | **递归 CTE 血缘下游** |

#### `src/contexthub/services/propagation_engine.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 94 | `UPDATE change_events SET delivery_status='retry', ... WHERE delivery_status='processing' AND claimed_at < NOW() - $1::interval` | 回收过期事件 |
| 95 | `UPDATE change_events SET delivery_status='processing', ... WHERE event_id IN (SELECT ... WHERE context_id = $1::uuid AND delivery_status IN ('pending','retry') AND next_retry_at <= NOW() ORDER BY timestamp ASC LIMIT $2) RETURNING *` | 领取事件（按 context） |
| 96 | `UPDATE change_events SET ... WHERE event_id IN (SELECT ... WHERE delivery_status IN ('pending','retry') ... LIMIT $1) RETURNING *` | 领取事件（全局） |
| 97 | `SELECT dependent_id, dep_type, pinned_version, created_at FROM dependencies WHERE dependency_id=$1 AND created_at <= $2 ORDER BY ...` | 查询依赖者 |
| 98 | `SELECT agent_id, pinned_version, created_at FROM skill_subscriptions WHERE skill_id=$1 AND created_at <= $2 ORDER BY ...` | 查询订阅者 |
| 99 | `UPDATE contexts SET status='stale', stale_at=NOW(), updated_at=NOW() WHERE id=$1 AND status NOT IN (...)` | 标记过期 |
| 100 | `INSERT INTO change_events (...) VALUES ($1,$2,'marked_stale','propagation_engine',$3)` | 变更事件 |
| 101 | `SELECT id, context_type, l0_content, l1_content, l2_content FROM contexts WHERE id = $1` | 加载源上下文 |
| 102 | `SELECT id, context_type, l2_content FROM contexts WHERE id = $1` | 加载依赖者 |
| 103 | `UPDATE contexts SET l0_content=$1, l1_content=$2, updated_at=NOW() WHERE id=$3` | 自动更新派生投影 |
| 104 | `UPDATE contexts SET l0_embedding = NULL WHERE id = $1` | 清除嵌入 |
| 105 | `UPDATE change_events SET delivery_status='processed', processed_at=NOW(), ... WHERE event_id=$1` | 完成事件 |
| 106 | `UPDATE change_events SET delivery_status='retry', ... next_retry_at = NOW() + make_interval(secs => LEAST(300, 5 * attempt_count)), ... WHERE event_id=$1` | **重试事件（make_interval）** |

#### `src/contexthub/api/routers/datalake.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 107 | `SELECT c.id FROM contexts c JOIN table_metadata tm ON ... WHERE c.uri=$1 AND tm.catalog=$2 ...` | SQL 上下文定位 |
| 108 | `SELECT c.id, c.uri, ... (SELECT jsonb_agg(jsonb_build_object(...)) FROM table_relationships tr ...) AS joins, (SELECT jsonb_agg(jsonb_build_object(...)) FROM (...) qt) AS top_templates FROM contexts c JOIN table_metadata tm ON ... WHERE c.id = ANY($1::uuid[]) AND tm.catalog=$2 ORDER BY array_position($1::uuid[], c.id)` | **SQL 上下文组装（jsonb_agg, jsonb_build_object, array_position）** |

#### `src/contexthub/api/routers/tools.py` & `contexts.py`

| # | SQL 语句摘要 | 用途 |
|---|---|---|
| 109 | `SELECT id, context_type FROM contexts WHERE uri=$1 AND status!='deleted'` | 路由分发 |
| 110 | `UPDATE contexts SET last_accessed_at = NOW() WHERE uri = $1` | 访问时间更新 |
| 111 | `SELECT context_type FROM contexts WHERE uri=$1 AND status != 'deleted'` | 类型检查 |

### 2.4 脚本与测试

| # | 文件 | SQL 语句 | 用途 |
|---|---|---|---|
| 112 | `scripts/demo_e2e.py` | `SET app.account_id = 'acme'` | 会话 GUC |
| 113 | `scripts/demo_e2e.py` | `INSERT INTO team_memberships ... ON CONFLICT DO NOTHING` | **UPSERT** |
| 114 | `tests/conftest.py` | `TRUNCATE contexts, ... CASCADE` | 测试清理 |

---

## 三、openGauss 7.0.0 兼容性逐项分析

### 风险等级说明

- 🔴 **阻断 (BLOCKER)** — 语法不支持，必须改写 SQL 否则无法执行
- 🟡 **需适配 (ADAPTATION)** — 功能支持但语法/扩展名不同，需修改代码
- 🟢 **兼容 (COMPATIBLE)** — 直接可用，无需修改

---

### 3.1 🔴 阻断级问题

#### 3.1.1 `INSERT ... ON CONFLICT` 不支持

**影响范围**: #63, #77, #79, #86, #87, #113（共 6 处）

**问题描述**:
openGauss 7.0.0 **不支持** PostgreSQL 的 `INSERT ... ON CONFLICT (columns) DO UPDATE SET ... / DO NOTHING` 语法。这是 PostgreSQL 9.5+ 引入的 upsert 语法，但 openGauss 至今未实现。

**涉及文件**:
- `src/contexthub/services/skill_service.py` — `INSERT INTO skill_subscriptions ... ON CONFLICT (agent_id, skill_id) DO UPDATE ... RETURNING *`
- `src/contexthub/services/catalog_sync_service.py` — 4 处 `ON CONFLICT ... DO UPDATE / DO NOTHING`
- `scripts/demo_e2e.py` — `ON CONFLICT DO NOTHING`

**openGauss 替代方案**:

openGauss 提供两种替代：

**方案 A: `ON DUPLICATE KEY UPDATE`（推荐简单场景）**
```sql
-- PostgreSQL 原始
INSERT INTO skill_subscriptions (agent_id, skill_id, pinned_version, account_id)
VALUES ($1, $2, $3, current_setting('app.account_id'))
ON CONFLICT (agent_id, skill_id)
DO UPDATE SET pinned_version = EXCLUDED.pinned_version
RETURNING *;

-- openGauss 改写
INSERT INTO skill_subscriptions (agent_id, skill_id, pinned_version, account_id)
VALUES ($1, $2, $3, current_setting('app.account_id'))
ON DUPLICATE KEY UPDATE pinned_version = EXCLUDED.pinned_version;
```

> ⚠️ **重要限制**: openGauss 的 `ON DUPLICATE KEY UPDATE` **不支持与 `RETURNING` 子句一起使用**。SQL #63 和 #77 同时使用了 `ON CONFLICT ... RETURNING *`，改写后需要拆分为两步操作（先 upsert，再 SELECT）。

**方案 B: `MERGE INTO`（推荐复杂场景）**
```sql
MERGE INTO skill_subscriptions t
USING (SELECT $1 AS agent_id, $2 AS skill_id, $3 AS pinned_version) s
ON (t.agent_id = s.agent_id AND t.skill_id = s.skill_id)
WHEN MATCHED THEN UPDATE SET pinned_version = s.pinned_version
WHEN NOT MATCHED THEN INSERT (agent_id, skill_id, pinned_version, account_id)
  VALUES (s.agent_id, s.skill_id, s.pinned_version, current_setting('app.account_id'));
```

**对于 `ON CONFLICT DO NOTHING`**: 可改写为 `INSERT ... ON DUPLICATE KEY UPDATE NOTHING`。

---

#### 3.1.2 `ON CONFLICT ... RETURNING *` + `(xmax = 0) AS is_new` 组合不可用

**影响范围**: #77（1 处，但属于核心同步逻辑）

**问题描述**:
`catalog_sync_service.py` 中的核心 upsert 语句同时使用了三个 openGauss 不兼容的特性：
1. `ON CONFLICT ... DO UPDATE SET ...` — 语法不支持
2. `RETURNING *` — 与 upsert 组合不支持
3. `(xmax = 0) AS is_new` — 利用 PostgreSQL 内部系统列判断是否是新插入行

**涉及代码**:
```sql
INSERT INTO contexts (...) VALUES (...)
ON CONFLICT (account_id, uri) DO UPDATE SET ...
RETURNING id, (xmax = 0) AS is_new
```

虽然 openGauss 支持 `xmax` 系统隐藏列，但由于 `ON CONFLICT` 和 `RETURNING` 的双重限制，此查询需要完全重构。

**改写建议**:
```sql
-- 先尝试 SELECT
SELECT id FROM contexts WHERE account_id = $4 AND uri = $1;
-- 如果不存在则 INSERT，如果存在则 UPDATE
-- 根据操作类型在应用层确定 is_new
```

---

#### 3.1.3 `CREATE EXTENSION IF NOT EXISTS vector` — pgvector 不存在

**影响范围**: #1（影响整个向量检索功能链：#10, #71, #73, #74, #75）

**问题描述**:
openGauss **不包含 pgvector 扩展**。openGauss 提供的是自研的 **DataVec** 扩展，虽然提供了相同的 `vector` 数据类型和 `<=>` 余弦距离操作符，但扩展名和安装方式不同。

**改写方法**:
```sql
-- PostgreSQL
CREATE EXTENSION IF NOT EXISTS vector;

-- openGauss
CREATE EXTENSION IF NOT EXISTS datavec;
```

DataVec 支持：
- ✅ `vector(1536)` 数据类型（最大 16000 维，索引最大 2000 维；1536 维在索引限制内）
- ✅ `<=>` 余弦距离操作符
- ✅ HNSW 索引 + `vector_cosine_ops` 操作符类
- ✅ `$1::vector` 类型转换
- ✅ `WITH (m=16, ef_construction=64)` 索引参数

因此向量相关的 DML（#71, #73, #75 等）**在安装 DataVec 后可直接使用**，无需修改。

---

#### 3.1.4 `CREATE EXTENSION IF NOT EXISTS pgcrypto` — pgcrypto 不存在

**影响范围**: #2 + 所有使用 `gen_random_uuid()` 的表定义（#3, #12, #17 等）

**问题描述**:
openGauss **不支持 pgcrypto 扩展**。`pgcrypto` 的控制文件在 openGauss 发行版中不包含，`CREATE EXTENSION pgcrypto` 会直接报错。

项目使用 `pgcrypto` 的唯一目的是提供 `gen_random_uuid()` 函数。

**改写方法**:
需确认 openGauss 是否内置 `gen_random_uuid()`。根据 openGauss 文档，`gen_random_uuid()` 从较新版本开始作为内置函数提供（无需扩展）。如果 7.0.0 版本中已内置，则只需删除 `CREATE EXTENSION IF NOT EXISTS pgcrypto` 语句即可。

若未内置，则需要使用 `uuid-ossp` 扩展或者 `sys_guid()` 函数替代：
```sql
-- 替代方案 1: 使用 uuid-ossp
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- 然后将 DEFAULT gen_random_uuid() 替换为 DEFAULT uuid_generate_v4()

-- 替代方案 2: 使用 sys_guid()
-- 将列默认值改为 DEFAULT sys_guid()
```

---

#### 3.1.5 `CREATE POLICY` 语法差异

**影响范围**: #6, #18, #22（共 3 处）

**问题描述**:
PostgreSQL 使用 `CREATE POLICY name ON table`，而 openGauss 使用 `CREATE ROW LEVEL SECURITY POLICY name ON table`。

**涉及代码**:
```sql
-- PostgreSQL 原始
CREATE POLICY tenant_isolation ON contexts
    USING (account_id = current_setting('app.account_id'))

-- openGauss 必须改为
CREATE ROW LEVEL SECURITY POLICY tenant_isolation ON contexts
    USING (account_id = current_setting('app.account_id'))
```

三处 `CREATE POLICY` 都需要加上 `ROW LEVEL SECURITY` 关键词。

---

#### 3.1.6 `CREATE TRIGGER ... EXECUTE FUNCTION` 语法差异

**影响范围**: #16（1 处）

**问题描述**:
PostgreSQL 11+ 推荐使用 `EXECUTE FUNCTION`，而 openGauss 的常规触发器只支持 `EXECUTE PROCEDURE` 语法。

**改写方法**:
```sql
-- PostgreSQL
CREATE TRIGGER trg_change_events_notify
AFTER INSERT ON change_events
FOR EACH ROW EXECUTE FUNCTION notify_change_event();

-- openGauss
CREATE TRIGGER trg_change_events_notify
AFTER INSERT ON change_events
FOR EACH ROW EXECUTE PROCEDURE notify_change_event();
```

---

#### 3.1.7 `make_interval()` 函数不支持

**影响范围**: #106（1 处）

**问题描述**:
openGauss 不支持 PostgreSQL 的 `make_interval(secs => ...)` 函数。

**涉及代码**:
```sql
next_retry_at = NOW() + make_interval(secs => LEAST(300, 5 * attempt_count))
```

**改写方法**:
```sql
-- 使用 interval 乘法替代
next_retry_at = NOW() + (LEAST(300, 5 * attempt_count) || ' seconds')::interval
-- 或
next_retry_at = NOW() + (LEAST(300, 5 * attempt_count) * interval '1 second')
```

---

### 3.2 🟡 需适配问题

#### 3.2.1 `LISTEN/NOTIFY` 及 `pg_notify` 可用性不确定

**影响范围**: 传播引擎核心机制（#15 触发器函数中的 `pg_notify` + `PropagationEngine` 的 `LISTEN`）

**问题描述**:
openGauss 的系统函数列表中包含 `pg_notify`（已在 7.0.0-RC1 文档中确认），但 `LISTEN` 语句的支持情况在官方文档中没有明确说明。openGauss 的函数文档声明"内置函数和操作符继承自开源 PG"，暗示可能支持，但需要实际验证。

**风险**:
如果 `LISTEN` 不支持，则整个事件传播的实时唤醒机制将失效。但由于 `PropagationEngine` 本身有周期性唤醒（`_periodic_wakeup`）作为兜底，系统不会完全中断，只是实时性会降低为定时轮询。

**建议**: 在 openGauss 环境中实测 `LISTEN 'context_changed'` 和 `pg_notify('context_changed', ...)` 是否可用。

#### 3.2.2 `array_position()` 函数可用性不确定

**影响范围**: #108（1 处）

**问题描述**:
`datalake.py` 中使用 `ORDER BY array_position($1::uuid[], c.id)` 来保持结果排序与输入数组一致。openGauss 的数组函数文档未明确列出 `array_position`，但其文档声明"函数继承自 PG"。

**改写方法（如不支持）**:
```sql
-- 使用子查询 + 生成序列替代
ORDER BY (SELECT i FROM generate_subscripts($1::uuid[], 1) i WHERE ($1::uuid[])[i] = c.id LIMIT 1)
```

#### 3.2.3 `jsonb_agg` 聚合函数可用性不确定

**影响范围**: #108（1 处）

**问题描述**:
`datalake.py` 的 SQL 上下文组装查询大量使用 `jsonb_agg(jsonb_build_object(...))` 子查询。`jsonb_build_object` 已确认支持，但 `jsonb_agg` 在 openGauss 文档中未明确提及。

**改写方法（如不支持）**:
可用 `json_agg` 替代后转 jsonb，或使用 `array_agg` + `array_to_json` 组合。

#### 3.2.4 asyncpg 驱动不兼容

**影响范围**: 整个数据访问层

**问题描述**:
标准 `asyncpg` 库是为 PostgreSQL 协议优化的，直接连接 openGauss 可能因认证协议差异（openGauss 使用 SHA256 认证）而失败。

**替代方案**:
华为提供了 **`async-gaussdb`** 库，这是 asyncpg 的 openGauss 适配分支，API 与 asyncpg 基本兼容，支持 SHA256 认证。

```bash
pip install async-gaussdb
```

代码中需要将 `import asyncpg` 替换为 `import async_gaussdb as asyncpg`（或按库的实际 API 调整）。

---

### 3.3 🟢 兼容项（无需修改）

以下 SQL 特性在 openGauss 7.0.0 中**已确认兼容**：

| 特性 | 涉及 SQL # | 说明 |
|---|---|---|
| `set_config()` / `current_setting()` | #33, #41, #45 等 | openGauss 完全支持 |
| `WITH RECURSIVE` 递归 CTE | #35, #92, #93 | openGauss 完全支持 |
| `UNION ALL` | #35, #92, #93 | 标准 SQL，完全支持 |
| `SELECT ... FOR UPDATE` 行锁 | #57 | openGauss 完全支持 |
| `UPDATE ... RETURNING` | #39, #44, #46, #47 等 | openGauss 支持（列存表除外） |
| `INSERT ... RETURNING` | #44, #50, #53 | openGauss 支持 |
| `DISTINCT ON (expression)` | #92, #93 | openGauss 完全支持 |
| `ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY` | #4, #5, #31, #32 | openGauss 完全支持 |
| `TIMESTAMPTZ` 数据类型 | 所有表 | openGauss 完全支持 |
| `JSONB` 数据类型与 `$n::jsonb` 转换 | #12, #23, #25, #79 | openGauss 完全支持 |
| `TEXT[]` 数组类型 | #3 (tags 字段) | openGauss 支持 |
| `= ANY($1)` 数组操作符 | #70, #92, #93 | openGauss 完全支持 |
| `SERIAL` 自增类型 | #11, #21, #26 | openGauss 完全支持 |
| `UUID` 数据类型 | 所有表 | openGauss 完全支持 |
| `COALESCE()`, `LEAST()`, `NOW()` | 多处 | openGauss 完全支持 |
| `LIKE` / `ILIKE` | #42, #72 | openGauss 完全支持 |
| `CASE WHEN ... THEN ... ELSE ... END` | #72, #90 | openGauss 完全支持 |
| 部分索引 (Partial Index) `CREATE INDEX ... WHERE ...` | #9, #13, #14 | openGauss 完全支持 |
| `PL/pgSQL` 函数语言 | #15 | openGauss 完全支持 |
| `TRUNCATE ... CASCADE` | #114 | openGauss 完全支持 |
| `DROP TABLE IF EXISTS ... CASCADE` | #29 | openGauss 完全支持 |
| `CHECK` 约束 | 多表 | openGauss 完全支持 |
| `UNIQUE` 约束 | 多表 | openGauss 完全支持 |
| `REFERENCES` 外键约束 | 多表 | openGauss 完全支持 |
| `$1::interval` 类型转换 | #94 | openGauss 完全支持 |
| `$1::uuid` 类型转换 | #95 | openGauss 完全支持 |
| `FLOAT` 数据类型 | #25 | openGauss 完全支持 |
| `HNSW` 索引（通过 DataVec） | #10 | openGauss DataVec 支持，语法一致 |
| `<=>` 余弦距离操作符（通过 DataVec） | #71 | openGauss DataVec 支持 |
| `vector_cosine_ops` 操作符类（通过 DataVec） | #10 | openGauss DataVec 支持 |

---

## 四、改写工作量汇总

| 严重程度 | 数量 | 涉及文件数 | 描述 |
|---|---|---|---|
| 🔴 阻断 | 7 类问题 | ~8 个文件 | 必须改写否则无法运行 |
| 🟡 适配 | 4 类问题 | ~5 个文件 | 需验证或小幅调整 |
| 🟢 兼容 | ~90+ 条 SQL | — | 无需修改 |

### 必须修改的文件清单

| 文件 | 修改项 |
|---|---|
| `alembic/versions/001_initial_schema.py` | `CREATE EXTENSION vector` → `datavec`; 删除 `pgcrypto`（或替换 UUID 方案）; `CREATE POLICY` → `CREATE ROW LEVEL SECURITY POLICY`（3处）; `EXECUTE FUNCTION` → `EXECUTE PROCEDURE`（1处） |
| `src/contexthub/services/skill_service.py` | `ON CONFLICT ... DO UPDATE ... RETURNING *` → `MERGE INTO` 或 `ON DUPLICATE KEY UPDATE` + 单独 SELECT |
| `src/contexthub/services/catalog_sync_service.py` | 4 处 `ON CONFLICT` → `MERGE INTO` 或 `ON DUPLICATE KEY UPDATE`; `RETURNING id, (xmax = 0) AS is_new` → 拆分为先查后改 |
| `src/contexthub/services/propagation_engine.py` | `make_interval(secs => ...)` → `(... \|\| ' seconds')::interval` |
| `src/contexthub/api/routers/datalake.py` | 若 `array_position` / `jsonb_agg` 不可用则需改写 |
| `src/contexthub/db/pool.py` / `repository.py` | `asyncpg` → `async-gaussdb` 驱动替换 |
| `scripts/demo_e2e.py` | `ON CONFLICT DO NOTHING` → `ON DUPLICATE KEY UPDATE NOTHING` |
| `pyproject.toml` / `requirements` | 依赖替换: `asyncpg` → `async-gaussdb`; `pgvector` python 包评估 |
| `docker-compose.yml` | `pgvector/pgvector:pg16` 镜像 → openGauss 7.0.0 镜像 |

---

## 五、迁移建议

### 5.1 推荐使用 PG 兼容模式

创建 openGauss 数据库时建议使用 **PG 兼容模式**：
```sql
CREATE DATABASE contexthub DBCOMPATIBILITY = 'PG';
```
这将最大程度保留 PostgreSQL 语法兼容性（如 `LIMIT/OFFSET`、`||` 字符串拼接、空字符串处理等）。

### 5.2 迁移优先级

1. **P0 — 驱动层**: 将 `asyncpg` 替换为 `async-gaussdb`，这是最基础的连接层变更
2. **P0 — 扩展替换**: `pgvector` → `datavec`，`pgcrypto` → 内置 UUID 或 `uuid-ossp`
3. **P0 — DDL 语法**: 修改 `CREATE POLICY` 和 `EXECUTE FUNCTION` 语法
4. **P1 — UPSERT 改写**: 6 处 `ON CONFLICT` 改为 `MERGE INTO` 或 `ON DUPLICATE KEY UPDATE`
5. **P1 — 函数替换**: `make_interval` → interval 表达式
6. **P2 — 验证**: `LISTEN/NOTIFY`、`array_position`、`jsonb_agg` 实测验证
7. **P3 — 基础设施**: Docker 镜像、CI/CD 流水线调整

### 5.3 建议引入兼容层

考虑在 `ScopedRepo` 层（`db/repository.py`）引入 SQL 方言抽象层，使同一份业务代码可以同时支持 PostgreSQL 和 openGauss：

```python
class SQLDialect:
    def upsert(self, table, conflict_cols, update_cols, returning=None):
        """根据数据库类型生成不同的 UPSERT 语法"""
        ...
```

---

## 六、结论

将 ContextHub 从 PostgreSQL 16 迁移到 openGauss 7.0.0 是**可行的**，但需要处理 **7 类阻断级兼容性问题**。其中最大的工程量在于：

1. **`ON CONFLICT` UPSERT 语法改写**（6 处，涉及 3 个核心服务文件，其中 2 处还组合了 `RETURNING`）
2. **扩展替换**（`pgvector` → `datavec`，`pgcrypto` → 内置方案）
3. **asyncpg 驱动替换**（→ `async-gaussdb`）

大部分标准 SQL（SELECT/UPDATE/INSERT/DELETE、JOIN、递归 CTE、行锁、RLS 等）在 openGauss 7.0.0 PG 兼容模式下可以直接使用，整体兼容性较好。预计核心代码改动集中在约 **8 个源文件**中，业务逻辑本身无需变更。
