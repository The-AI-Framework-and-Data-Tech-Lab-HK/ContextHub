
## 为ContextHub新增openGauss后端

### 一、openGauss server配置

*   参考`docs/setup/opengauss-setup-guide-zh.md`新增server后端
*   CREATE DATABASE时需要设定DBCOMPATIBILITY = 'PG'模式, 避免空字符串被interpret成NULL。

### 二、Extension替换

*   原ContextHub项目依赖pgvector+pgcrypto两个插件，这两个库在opengauss都不支持
*   解决方案：opengauss 7.0.0原生支持DataVec，可以替换pgvector；opengauss不支持pgcrypto，替代方案uuid-ossp在官方镜像中也缺失无法安装，最终手动自定义实现`gen_random_uuid`函数，规避pgcrypto extension

### 三、Python Driver兼容

*   原ContextHub使用asyncpg库与db后端交互，但是asyncpg不支持opengauss的vector数据格式
    *   报错信息为`message: unhandled standard data type 'vector' (OID 8305)`, 复现脚本为`opengauss/vector_asyncpg.py`
    *   gaussdb有一个自己维护的`async_gaussdb`库，但是一样不支持vector格式, 验证脚本为`opengauss/vector_async_gaussdb.py`
*   解决方案：新增db兼容层，PG后端仍然使用asyncpg，OpenGauss后端切换到psycopg3连接
    *   psycopg3 的位置参数语法与asyncpg完全不同，一个是%s一个是$n，用正则匹配转换
    *   兼容层处理语法转换，对外封装暴露统一的fetch/fetchrow/fetchall/execute接口
    *   相关实现在`src/contexthub/db/repository.py`

### 四、SQL Dialect转写

*   原ContextHub使用postgres方言的SQL，多种语言特性与opengauss不兼容
*   例如，openGauss不支持PG的INSERT ON CONFLICT (需要重写为ON DUPLICATE KEY UPDATE), 且不能与RETURNING语句, ROW POLICY同时使用
*   全项目约20+条需要重写, 详细情况可见于报告`opengauss-compatibility-report.md`

### 整体完成度

*   步骤一、二、三进度100%，目前demo `opengauss/demo_e2e_opengauss.py` 可以成功执行前3个steps
*   步骤四进度50%, 正在处理demo的第四个step的SQL转写，具体可见`ContextHub/src/contexthub/services/skill_service.py`的FIXME


---

<div align="center">

<img src="figures/logo2.jpeg" width="200">

### ContextHub: Unified Context Management <br> for Multi-Agent Collaboration

A context-governance engine built on a **filesystem paradigm** with **LLM-native commands**.
Agents navigate memories, skills, documents, and data-lake metadata through familiar operations
(`ls`, `read`, `grep`, `stat`) over `ctx://` URIs — with version control, visibility boundaries,
change propagation, and cross-agent sharing.

Built on FastAPI + PostgreSQL. Single database. No external vector store. No message queue.

English | [中文](README_zh.md)
</div>

---

## Why ContextHub? 🔎

When multiple AI agents collaborate on the same business entities, their contexts are siloed, unversioned, and disconnected:

> * **79% of multi-agent failures** stem from coordination problems, not technical bugs ([Zylos Research, 2026](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical)).
> * **36.9% of failures** come from inter-agent misalignment — agents ignoring, duplicating, or contradicting each other's work ([Cemri et al., 2025](https://arxiv.org/abs/2503.13657)).

These are structural deficits in system architecture — they cannot be fixed by improving individual model capabilities. ContextHub addresses this by unifying four types of context under one governance layer.

## What Does ContextHub Manage? 📦

| Context Type | What It Is | Example |
|---|---|---|
| **Memory** | Facts, patterns, and decisions an agent learns during conversations | A SQL query pattern that worked for monthly sales reports |
| **Skill** | Reusable capabilities that agents publish, version, and subscribe to | A "SQL Generator" skill — subscribers get notified on breaking changes |
| **Resource** | Documents that agents read, understand, and retrieve | API docs, runbooks, or policy documents referenced during tasks |
| **Data-Lake Metadata** | Structured metadata for lakehouse tables — schemas, columns, lineage | Table `orders(user_id, amount, created_at)` and its upstream/downstream dependencies |

All four are managed under a unified `ctx://` URI namespace with the same versioning, visibility, and propagation semantics.

> For a detailed analysis of research gaps in each context type, see [Research Positioning](docs/research/research-positioning.md).

## Core Capabilities ✨

| Capability | What It Solves |
|---|---|
| **Filesystem Paradigm** | All context types managed as files under `ctx://` URIs — one model for memories, skills, documents, and table metadata |
| **LLM-native Commands** | Agents use `ls`, `read`, `grep`, `stat` — LLMs already understand file operations, no custom API needed |
| **Multi-Agent Collaboration** | Team hierarchy with visibility inheritance (child reads parent, parent doesn't see child); memory promotion `private → team → org` with `derived_from` lineage |
| **Version Management** | Pin agents to stable versions; `is_breaking` flag prevents silent breakage; immutable published versions |
| **Change Propagation** | Upstream changes auto-notify all downstream dependents — no polling, no "latest version wins" |
| **L0/L1/L2 Layered Retrieval** | Vector search → BM25 rerank → on-demand full content; **60–80% token reduction** vs. flat retrieval |
| **Tenant Isolation** | Row-Level Security on all tables; request-scoped tenant binding |
| **PostgreSQL-centric Single DB** | ACID + RLS + LISTEN/NOTIFY + pgvector in one database; no dual-write, no message queue |

## Architecture 🏛️

```
         Agents (via OpenClaw Plugin / SDK)
              │
              ▼
    ContextHub Server (FastAPI)
    ├── ContextStore       — ctx:// URI routing
    ├── MemoryService      — promote, lineage, team sharing
    ├── SkillService       — publish, subscribe, version resolution
    ├── RetrievalService   — pgvector + BM25 rerank
    ├── PropagationEngine  — outbox, retry, dependency dispatch
    └── ACLService         — visibility / write permissions
              │
              ▼
    PostgreSQL + pgvector  (single DB: metadata + content + vectors + events)
```

**Single database. No external vector store. No message queue.** This eliminates dual-write consistency problems and minimizes infrastructure complexity for on-premise deployment.

---

## Quick Start 🚀

### Prerequisites

- **Python 3.12+**
- **PostgreSQL 16** with **pgvector** extension

### Step 1: Install PostgreSQL + pgvector

<details>
<summary><strong>macOS (Homebrew)</strong></summary>

```bash
brew install postgresql@16
brew install pgvector
brew services start postgresql@16
```

</details>

<details>
<summary><strong>Linux (Ubuntu / Debian)</strong></summary>

```bash
# Add PostgreSQL APT repository
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list

sudo apt update
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo systemctl start postgresql
```

</details>

Verify PostgreSQL is running:

```bash
pg_isready
# Expected: "accepting connections"
```

### Step 2: Create Database

```bash
# macOS (Homebrew): psql postgres
# Linux: sudo -u postgres psql
psql postgres
```

Inside the `psql` shell:

```sql
CREATE USER contexthub WITH PASSWORD 'contexthub' SUPERUSER;
CREATE DATABASE contexthub OWNER contexthub;
\c contexthub
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
\q
```

> `SUPERUSER` is required because the schema uses `FORCE ROW LEVEL SECURITY`. This is fine for local development.

### Step 3: Install & Start ContextHub

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
pip install greenlet
pip install -e sdk/

# Run database migrations
alembic upgrade head

# Start the server
uvicorn contexthub.main:app --port 8000
```

Verify:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

API docs available at http://localhost:8000/docs.

### Step 4: Try the Python SDK

```python
from contexthub_sdk import ContextHubClient

client = ContextHubClient(base_url="http://localhost:8000", api_key="changeme")

# Store a private memory
memory = await client.add_memory(
    content="SELECT date_trunc('month', created_at), SUM(amount) FROM orders GROUP BY 1",
    tags=["sql", "sales"],
)

# Promote to team-shared knowledge
promoted = await client.promote_memory(uri=memory.uri, target_team="engineering")

# Semantic search across all visible contexts
results = await client.search("monthly sales summary", top_k=5)
```

ContextHub also integrates directly with agent frameworks like [OpenClaw](https://github.com/anthropics/openclaw) as a drop-in context engine — making context governance transparent to agent code. See [Integration with OpenClaw](#integration-with-openclaw-) below.

For the full E2E demo and integration tests, see [Local Setup & E2E Verification Guide](docs/setup/local-setup&end2end-verification-guide.md).

---

## Integration with OpenClaw 🦞

ContextHub is designed as the **context engine** for [OpenClaw](https://github.com/anthropics/openclaw) — replacing its built-in engine with enterprise-grade context governance.

```bash
# One-command install
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

**What happens automatically (no agent code changes):**

| Event | ContextHub Action |
|-------|-------------------|
| Agent receives a prompt | `assemble()` — searches all visible contexts and injects relevant ones into the system prompt |
| Agent completes a response | `afterTurn()` — extracts reusable facts and stores them as private memories |

**7 agent tools available in every session:**

`ls` · `read` · `grep` · `stat` · `contexthub_store` · `contexthub_promote` · `contexthub_skill_publish`

### Multi-Agent Collaboration in Action

```
Org: engineering/backend  ← query-agent        Org: data/analytics  ← analysis-agent
                                                     (also engineering member)
```

```
1. query-agent stores a SQL pattern as private memory

2. query-agent promotes it to engineering team
   → ctx://team/engineering/shared_knowledge/monthly-sales-pattern

3. analysis-agent asks "How to query monthly sales?"
   → ContextHub auto-recalls the promoted pattern via assemble()
   → zero manual sharing needed

4. query-agent publishes breaking Skill v2
   → analysis-agent (pinned to v1) continues using v1 stably
   → advisory: "v2 available with breaking changes"
```

> **What makes this different from a shared document?**
> ContextHub enforces visibility boundaries, tracks `derived_from` lineage,
> and propagates changes through dependency graphs — not just "latest version wins."

For full setup instructions, see the [OpenClaw Integration Guide](docs/setup/openclaw-integration-guide.md).

---

## Roadmap 🗺️

- [x] **Phase 1 — MVP Core** ✅
  Context store (`ctx://` URI routing), memory / skill / retrieval / propagation services, ACL with RLS + team hierarchy, Python SDK, OpenClaw context-engine plugin, data lake carrier, Tier 3 integration tests (P-1~P-8, C-1~C-5, A-1~A-4)
- [ ] **Phase 2 — Explicit ACL & Audit** — ACL allow/deny/field mask overlay, audit logging, cross-team sharing
- [ ] **Phase 3 — Feedback & Lifecycle** — Quality signals, automatic lifecycle transitions, long doc retrieval
- [ ] **Phase 4 — Quantitative Evaluation (ECMB)** — SQL accuracy benchmarks, L0/L1/L2 vs. flat RAG A/B experiments
- [ ] **Phase 5 — Production Hardening** — Multi-instance (`SKIP LOCKED`), MCP Server, real catalog connectors

## Documentation 📄

| Document | Description |
|----------|-------------|
| [OpenClaw Integration Guide](docs/setup/openclaw-integration-guide.md) | Full 5-terminal setup for ContextHub + OpenClaw |
| [Local Setup & E2E Verification](docs/setup/local-setup&end2end-verification-guide.md) | Dev environment, migrations, E2E demo |
| [MVP Verification Plan](docs/mvp%20verification/mvp-verification-plan.md) | Three-layer verification: tests → API demo → runtime contract |
| [Developer Guide](docs/design%20and%20development/development-guide.md) | API overview, SDK reference, tech stack, project structure |

## References 📚

- [AI Agent Memory Architectures](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical) — Zylos Research, 2026
- [Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026
- [Governed Memory](https://arxiv.org/abs/2603.17787) — Taheri, 2026
- [Collaborative Memory](https://arxiv.org/abs/2505.18279) — Multi-user memory sharing with dynamic ACL
- [OpenViking](https://github.com/volcengine/OpenViking) — Core design inspiration (personal-edition context management)
- [Model Context Protocol](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024

## License ⚖️

[Apache License 2.0](LICENSE)
