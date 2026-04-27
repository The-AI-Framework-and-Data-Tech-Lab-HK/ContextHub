
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

> * **41–87% of multi-agent LLM systems fail** in production, with failures clustering into system design issues, inter-agent misalignment, and task verification — structural problems, not individual model deficiencies ([Cemri et al., NeurIPS 2025](https://arxiv.org/abs/2503.13657)).

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
```

Configure `.env` (copy from the example, then fill in your API key and adjust model settings if needed):

```bash
cp .env.example .env
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_KEY` | Yes | `changeme` | Server auth — all SDK/HTTP requests must send this as `X-API-Key` header |
| `OPENAI_API_KEY` | For LLM-backed features | *(empty)* | Enables vector embeddings for `search` and the chat-based tree builder / tree selector used by long-document ingestion and retrieval; without it, vector search is disabled and long-document ingestion returns `503` |
| `OPENAI_BASE_URL` | No | `https://api.openai.com/v1` | Shared base URL for both chat and embedding requests; can point to an OpenAI-compatible endpoint |
| `CHAT_MODEL` | No | `gpt-4o-mini` | Chat model used by long-document tree construction and tree-based section selection |
| `EMBEDDING_MODEL` | No | `text-embedding-3-small` | Embedding model used for L0/query embeddings during retrieval |
| `EMBEDDING_DIMENSIONS` | No | `1536` | Target stored vector dimension; exact matches are accepted, shorter compatible embeddings are zero-padded, and larger embeddings are rejected |

By default, ContextHub uses the official OpenAI endpoint with `gpt-4o-mini` for chat and `text-embedding-3-small` for embeddings. To switch to another OpenAI-compatible deployment, change `OPENAI_BASE_URL`, `CHAT_MODEL`, and `EMBEDDING_MODEL` together. If your provider returns a shorter embedding than the configured `EMBEDDING_DIMENSIONS`, ContextHub pads it with trailing zeros before storage and retrieval so it can still fit the configured pgvector column.

```bash
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
import asyncio
from contexthub_sdk import ContextHubClient

async def main():
    async with ContextHubClient(
        url="http://localhost:8000",
        api_key="changeme",
        account_id="acme",
        agent_id="query-agent",
    ) as client:
        # Store a private memory
        memory = await client.memory.add(
            content="SELECT date_trunc('month', created_at), SUM(amount) FROM orders GROUP BY 1",
            tags=["sql", "sales"],
        )

        # Promote to team-shared knowledge
        promoted = await client.memory.promote(uri=memory.uri, target_team="engineering")

        # Semantic search across all visible contexts
        results = await client.search("monthly sales summary", top_k=5)

asyncio.run(main())
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
- [x] **Phase 2 — Explicit ACL & Audit** ✅
  ACL allow/deny/field mask overlay, tiered audit logging, cross-team share grants, Admin API + audit query, SDK extensions, Tier 3 integration tests (A-5~A-15)
- [x] **Phase 3 — Feedback & Lifecycle** — Quality signals, automatic lifecycle transitions, long doc retrieval
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

- [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) — Cemri et al., NeurIPS 2025: MAST taxonomy of 14 failure modes across 7 MAS frameworks
- [Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026
- [Governed Memory](https://arxiv.org/abs/2603.17787) — Taheri, 2026
- [Collaborative Memory](https://arxiv.org/abs/2505.18279) — Multi-user memory sharing with dynamic ACL
- [OpenViking](https://github.com/volcengine/OpenViking) — Core design inspiration (personal-edition context management)
- [Model Context Protocol](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024

## License ⚖️

[Apache License 2.0](LICENSE)
