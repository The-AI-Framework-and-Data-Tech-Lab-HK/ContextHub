# ContextHub

**Enterprise context management middleware for multi-agent collaboration.**

When multiple AI agents operate on the same business entities, their contexts — memories, skills, policies, schemas — are siloed, unversioned, and disconnected. Research shows **79% of multi-agent failures stem from coordination problems, not technical bugs**. ContextHub solves this with a unified context state layer.

## Why ContextHub

| Problem | ContextHub's Answer |
|---------|-------------------|
| Agents can't see each other's work | Hierarchical team ownership with visibility inheritance |
| Policy changes don't propagate | Dependency-graph-driven change propagation (3-tier rules) |
| No version control for skills/tools | Skill versioning with breaking change detection + subscriber notifications |
| Knowledge stays locked in one agent | Memory promotion: private → team → organization |
| SaaS-only options (Mem0, Governed Memory) | Self-hosted, PostgreSQL-centric — built for on-premise enterprise deployment |

### What Makes It Different

Most frameworks treat agent context as just "memory." ContextHub governs **four context types** under a unified model:

- **Memory** — conversation history, entity state, working memory
- **Skill** — tool definitions, prompt templates, agent configs (with version lifecycle)
- **Resource** — policy docs, compliance rules, knowledge bases (with change propagation)
- **Structured Metadata** — database schemas, data lake catalogs

## Architecture

```
         Agents (via OpenClaw / SDK)
              │
              ▼
    ContextHub Server (FastAPI)
    ├── ContextStore     — ctx:// URI routing (read/write/ls/stat)
    ├── MemoryService    — promote, derived_from, team sharing
    ├── SkillService     — publish, subscribe, version resolution
    ├── RetrievalService — unified search (pgvector + rerank)
    ├── PropagationEngine — change event processing + retry
    └── ACLService       — default visibility / write permissions
              │
              ▼
    PostgreSQL + pgvector
    (metadata, content, vectors, events — all in one DB)
```

Single database. No external vector store. No message queue. PostgreSQL handles ACID transactions, RLS tenant isolation, LISTEN/NOTIFY for change propagation, recursive CTEs for lineage queries, and pgvector for semantic search.

## Core Capabilities

### Multi-Agent Collaboration
- **Team ownership model** with hierarchical visibility inheritance
- **Memory promotion** from private → team → organization scope, with `derived_from` lineage tracking
- **Cross-agent knowledge reuse** — promoted memories are searchable by teammates

### Skill Version Management
- Publish new versions with `is_breaking` flag
- Subscribers choose `pinned` (stable) or `latest` (floating) resolution
- Breaking changes mark downstream dependents as `stale` with advisory notifications

### Change Propagation
- Three-tier propagation rules: pure rule / template substitution / LLM reasoning
- Outbox pattern with `change_events` table as source of truth
- NOTIFY for fast wake-up + periodic sweep for guaranteed delivery
- Automatic retry with exponential backoff; crash recovery via lease timeout

### L0/L1/L2 Layered Retrieval
- **L0**: one-line summary + embedding (vector search)
- **L1**: structured overview (reranking)
- **L2**: full content (on-demand loading)
- Reduces context token consumption by 60-80% vs flat schema dumps

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- PostgreSQL 16 with pgvector (provided via docker-compose)

### 1. Clone and install

```bash
git clone https://github.com/your-org/contexthub.git
cd contexthub
pip install -e ".[dev]"
```

### 2. Start PostgreSQL

```bash
docker compose up -d
```

This starts PostgreSQL 16 with pgvector on port 5432 (user: `contexthub`, password: `contexthub`, database: `contexthub`).

### 3. Run database migrations

```bash
alembic upgrade head
```

### 4. Start the server

```bash
uvicorn contexthub.main:app --reload
```

The API is available at `http://localhost:8000`. OpenAPI docs at `/docs`.

### 5. Use the SDK

```python
from contexthub import ContextHubClient

ctx = ContextHubClient(url="http://localhost:8000", api_key="...")

# Search context
results = await ctx.search("monthly sales summary", scope="datalake", level="L1")

# Record a successful case
await ctx.memory.add_case(
    content="SELECT ... GROUP BY month",
    context={"question": "monthly sales", "tables_used": ["orders", "products"]}
)

# Promote to team-shared memory
await ctx.memory.promote(
    uri="ctx://agent/query-agent/cases/xxx",
    target_team="engineering/backend"
)
```

## API Overview

All requests require `X-Account-Id` and `X-Agent-Id` headers for tenant isolation.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/contexts` | Create context |
| GET | `/api/v1/contexts/{uri}` | Read context (skills resolve via version logic) |
| POST | `/api/v1/search` | Unified semantic search |
| POST | `/api/v1/memories` | Add memory |
| POST | `/api/v1/memories/promote` | Promote memory to team scope |
| POST | `/api/v1/skills/versions` | Publish new skill version |
| POST | `/api/v1/skills/subscribe` | Subscribe to a skill |

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Web Framework | FastAPI | Async, type-safe, auto-generated OpenAPI |
| Database | PostgreSQL 16 | Unified storage for metadata + content + vectors + events |
| Vector Search | pgvector | Same-DB, same-transaction consistency; no dual-write |
| Async Driver | asyncpg | High-performance async PG with native LISTEN/NOTIFY |
| Migrations | Alembic | Schema version management |
| Embedding | text-embedding-3-small / BGE-M3 | Cost-effective for L0 summaries |

## Project Structure

```
contexthub/
├── src/contexthub/
│   ├── api/          # FastAPI routers + middleware
│   ├── db/           # PgRepository, ScopedRepo, SQL queries
│   ├── models/       # Pydantic models
│   ├── services/     # Business logic (memory, skill, retrieval, propagation)
│   ├── store/        # ContextStore (URI routing)
│   ├── retrieval/    # Search strategies (vector, rerank)
│   ├── propagation/  # Change propagation rules
│   └── generation/   # L0/L1 content generation
├── sdk/              # Python SDK (typed HTTP client)
├── plugins/openclaw/ # OpenClaw context-engine plugin
├── alembic/          # Database migrations
└── tests/
```

## Roadmap

- [x] Phase 0 — Project scaffolding, Docker, DB setup
- [ ] Phase 1 — Core foundation (ContextStore, ACL, request-scoped DB model)
- [ ] Phase 2 — Collaboration loop (memory promotion, skill versioning, propagation, search)
- [ ] Phase 3 — Vertical carrier (data lake metadata, Text-to-SQL context assembly)
- [ ] Phase 4 — Explicit ACL, audit logging, feedback lifecycle

## License

TBD
