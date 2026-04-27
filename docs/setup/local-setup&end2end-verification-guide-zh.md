# ContextHub MVP — 本地环境搭建与端到端验证指南

> 无需 Docker。使用 Homebrew 在 macOS 本地安装 PostgreSQL + pgvector。

## 前置条件

- **macOS**，已安装 [Homebrew](https://brew.sh)
- **Python 3.12+**
- **Node.js 18+**（仅 Bridge / Part B 验证需要）
- 运行项目命令前需**退出 Conda 环境**

---

## 1. 通过 Homebrew 安装 PostgreSQL + pgvector

```bash
brew install postgresql@16
brew install pgvector
```

启动 PostgreSQL 服务：

```bash
brew services start postgresql@16
```

验证是否正在运行：

```bash
pg_isready
# 预期输出: "accepting connections"
```

---

## 2. 创建数据库和用户

```bash
psql postgres
```

在 `psql` 交互界面中执行：

```sql
CREATE USER contexthub WITH PASSWORD 'contexthub' SUPERUSER;
CREATE DATABASE contexthub OWNER contexthub;
\c contexthub
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
\q
```

> **说明**：`SUPERUSER` 权限是必需的，因为数据库 schema 使用了 `FORCE ROW LEVEL SECURITY`。
> 本地开发环境使用 superuser 没有问题。

验证连接：

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "SELECT 1"
```

---

## 3. 搭建 Python 环境

**重要**：先退出 Conda 环境，避免 PATH 冲突：

```bash
conda deactivate
```

然后创建并激活项目虚拟环境：

```bash
cd /path/to/ContextHub

python3 -m venv .venv
source .venv/bin/activate
```

安装所有依赖：

```bash
# 主项目（含开发依赖）
pip install -e ".[dev]"

# greenlet 是 SQLAlchemy 异步引擎（Alembic 使用）的必要依赖
pip install greenlet

# SDK
pip install -e sdk/

# OpenClaw Plugin
pip install -e plugins/openclaw/
```

---

## 4. 执行数据库迁移

```bash
alembic upgrade head
```

预期输出：

```
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema...
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, Force row level security
```

此步骤会创建所有数据表，并插入种子数据（团队层级 + Agent 成员关系）。

---

## 5. 启动 ContextHub Server

**终端 1**（保持运行，不要关闭）：

```bash
source .venv/bin/activate
uvicorn contexthub.main:app --port 8000
```

预期输出：

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

在另一个终端验证：

```bash
curl http://localhost:8000/health
# 预期: {"status":"ok"}
```

也可以在浏览器中打开 http://localhost:8000/docs 查看所有 API 路由。

> **说明**：如果 `.env` 中未设置 `OPENAI_API_KEY`，Server 会使用 NoOp embedding 客户端。
> 依赖 embedding 的功能（向量检索、sql-context）会返回有限的结果。这在本地 MVP 验证中是预期行为。

---

## 6. 运行端到端 Demo

**终端 2**：

```bash
cd /path/to/ContextHub
source .venv/bin/activate
python scripts/demo_e2e.py
```

Demo 脚本会自动确保 `query-agent` 拥有 `engineering` 团队的写权限。整个流程包含 7 个步骤：

| 步骤 | 描述 |
|------|------|
| 1 | `query-agent` 写入私有记忆 |
| 2 | `query-agent` 创建并发布 Skill v1 |
| 3 | `query-agent` 将记忆晋升到 `team/engineering` |
| 4 | `analysis-agent` 检索到共享记忆，并订阅 Skill |
| 5 | `query-agent` 发布 breaking Skill v2 |
| 6 | 验证传播：stale/advisory 检测 |
| 7 | （垂直载体）Catalog 同步 + sql-context 查询 |

预期最终输出：

```
============================================================
  MVP Demo Complete
============================================================
  - Private memory created: ctx://agent/query-agent/memories/...
  - Promoted to team: ctx://team/engineering/shared_knowledge/...
  - Skill v1 + v2 published, breaking propagation triggered
  - Cross-agent visibility verified
  - Catalog sync + sql-context demonstrated
```

---

## 7. 运行测试

### 7a. 快速单元测试（无需数据库）

```bash
pytest tests/ -v
```

所有非集成测试会运行，集成测试会被自动跳过。

### 7b. 数据库集成测试（需要 PostgreSQL 运行中）

```bash
CONTEXTHUB_INTEGRATION=1 pytest \
  tests/test_integration_propagation.py \
  tests/test_integration_collaboration.py \
  tests/test_integration_visibility.py \
  tests/test_datalake.py \
  -v
```

---

## 8. Bridge 验证（Part B）

### 8a. TypeScript 编译

```bash
cd bridge
npm install
npx tsc --noEmit   # 仅类型检查
npx tsc            # 完整编译 → dist/
cd ..
```

预期：无编译错误；`bridge/dist/` 目录下生成 `index.js`、`bridge.js` 及对应的 `.d.ts` 文件。

### 8b. Python Sidecar

确保 ContextHub Server 正在运行（终端 1），然后：

```bash
python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000
```

验证：

```bash
curl http://localhost:9100/health
# 预期: {"status":"ok"}

curl http://localhost:9100/info
# 预期: ContextEngine info JSON

curl http://localhost:9100/tools
# 预期: tool definitions 列表

curl -X POST http://localhost:9100/assemble \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "test", "messages": [], "tokenBudget": 4000}'
# 预期: 包含 systemPromptAddition 的 JSON
```

---

## 9. 手动 API 验证（可选）

```bash
# 触发全量 catalog 同步
curl -X POST http://localhost:8000/api/v1/datalake/sync \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent" \
  -H "Content-Type: application/json" \
  -d '{"catalog": "mock"}'

# 列出已同步的表
curl http://localhost:8000/api/v1/datalake/mock/prod \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# 获取单张表的完整上下文
curl http://localhost:8000/api/v1/datalake/mock/prod/orders \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# 查询表血缘关系
curl http://localhost:8000/api/v1/datalake/mock/prod/orders/lineage \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# SQL 上下文组装
curl -X POST http://localhost:8000/api/v1/search/sql-context \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent" \
  -H "Content-Type: application/json" \
  -d '{"query": "每个用户有多少订单？", "catalog": "mock", "top_k": 3}'
```

---

## 重置数据库

如需从干净状态重新开始（例如重新跑 Demo）：

```bash
# 方案 A：仅清除业务数据（保留种子数据）
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  TRUNCATE contexts, dependencies, change_events,
           table_metadata, lineage, table_relationships,
           query_templates, skill_versions, skill_subscriptions
  CASCADE;
"

# 方案 B：完全重置数据库（重新执行迁移 + 种子数据）
psql postgres -c "DROP DATABASE IF EXISTS contexthub;"
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
alembic upgrade head
```

---

## 停止 / 重启 PostgreSQL

```bash
brew services stop postgresql@16     # 停止
brew services start postgresql@16    # 启动
brew services restart postgresql@16  # 重启
```

---

## 验证清单

| # | 验证项 | 命令 / 方法 | 预期结果 |
|---|--------|------------|---------|
| 1 | PostgreSQL 运行中 | `pg_isready` | accepting connections |
| 2 | 迁移成功 | `alembic upgrade head` | 无报错 |
| 3 | Server 启动 | `uvicorn contexthub.main:app --port 8000` | 监听 :8000 |
| 4 | 健康检查 | `curl localhost:8000/health` | `{"status":"ok"}` |
| 5 | API 文档可见 | 浏览器打开 `localhost:8000/docs` | 可看到 datalake 路由 |
| 6 | 单元测试通过 | `pytest tests/ -v` | 全部 PASSED |
| 7 | 集成测试通过 | `CONTEXTHUB_INTEGRATION=1 pytest ...` | 全部 PASSED |
| 8 | E2E Demo 完成 | `python scripts/demo_e2e.py` | 7 步全部成功 |
| 9 | TS Bridge 编译通过 | `cd bridge && npx tsc` | 无报错 |
| 10 | Sidecar 启动 | `python bridge/src/sidecar.py` | 监听 :9100 |
| 11 | Sidecar 健康检查 | `curl localhost:9100/health` | `{"status":"ok"}` |

---

## 常见问题排查

### `unrecognized configuration parameter "app.account_id"`

`contexthub` 数据库用户不是 superuser。修复方法：

```bash
psql postgres -c "ALTER USER contexthub WITH SUPERUSER;"
```

### `No module named 'greenlet'`

```bash
pip install greenlet
```

### Server 启动时传播引擎报错（`Failed to update embedding`）

数据库中有之前运行残留的 `change_events`，传播引擎尝试处理但因缺少 embedding 客户端而失败。清除残留事件：

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  UPDATE change_events
  SET delivery_status = 'processed', processed_at = NOW()
  WHERE delivery_status IN ('pending', 'retry', 'processing');
"
```

或者在 `.env` 中禁用传播引擎：

```
PROPAGATION_ENABLED=false
```

### Demo 第 2 步返回 403 Forbidden

`query-agent` 缺少 `engineering` 团队的直接成员记录。更新后的 Demo 脚本会自动处理此问题。如使用旧版脚本，手动修复：

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
  VALUES ('query-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE)
  ON CONFLICT DO NOTHING;
"
```

### `alembic` 或 `uvicorn` 调用的是 Conda 而非 .venv 的版本

务必先退出 Conda 环境：

```bash
conda deactivate
source .venv/bin/activate
```

用 `which alembic` 验证——应指向 `.venv/bin/alembic`，而非 `/opt/anaconda3/bin/alembic`。

