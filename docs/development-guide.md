# ContextHub — Developer Guide

> API reference, tech stack, and project structure for ContextHub contributors.
>
> For local development setup, see [Local Setup & E2E Verification Guide](local-setup&end2end-verification-guide.md).
> For OpenClaw integration setup, see [OpenClaw Integration Guide](openclaw-integration-guide.md).

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose (or Homebrew-installed PostgreSQL on macOS)
- PostgreSQL 16 with pgvector

### 1. Clone and install

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
pip install -e ".[dev]"
```

### 2. Start PostgreSQL

```bash
docker compose up -d
```

This starts PostgreSQL 16 with pgvector on port 5432 (user: `contexthub`, password: `contexthub`, database: `contexthub`).

For macOS without Docker, see the [Local Setup Guide](local-setup&end2end-verification-guide.md) for Homebrew-based PostgreSQL installation.

### 3. Run database migrations

```bash
alembic upgrade head
```

### 4. Start the server

```bash
uvicorn contexthub.main:app --reload
```

The API is available at `http://localhost:8000`. OpenAPI docs at `/docs`.

## API Overview

All requests require `X-Account-Id`, `X-Agent-Id`, and `X-API-Key` headers for tenant isolation and authentication.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/contexts` | Create context |
| GET | `/api/v1/contexts/{uri}` | Read context (skills resolve via version logic) |
| PATCH | `/api/v1/contexts/{uri}` | Update context (optimistic locking via `If-Match`) |
| DELETE | `/api/v1/contexts/{uri}` | Logical delete |
| POST | `/api/v1/search` | Unified semantic search |
| POST | `/api/v1/memories` | Add private memory |
| POST | `/api/v1/memories/promote` | Promote memory to team scope |
| POST | `/api/v1/skills/versions` | Publish new skill version |
| POST | `/api/v1/skills/subscribe` | Subscribe to a skill |
| POST | `/api/v1/tools/{ls,read,grep,stat}` | Agent tool-use endpoints |

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Web Framework | FastAPI | Async, type-safe, auto-generated OpenAPI |
| Database | PostgreSQL 16 | Unified storage for metadata + content + vectors + events |
| Vector Search | pgvector | Same-DB, same-transaction consistency; no dual-write |
| Async Driver | asyncpg | High-performance async PG with native LISTEN/NOTIFY |
| Migrations | Alembic | Schema version management |
| Embedding | text-embedding-3-small (1536-dim) | Cost-effective for L0 summaries |
| HTTP Client | httpx | Lightweight async HTTP for embedding API calls |
| Validation | Pydantic v2 | Request/response models with automatic validation |

## Project Structure

```
contexthub/
├── src/contexthub/
│   ├── api/              # FastAPI routers + middleware + dependency injection
│   ├── db/               # PgRepository, ScopedRepo (request-scoped DB executor)
│   ├── models/           # Pydantic models
│   ├── services/         # Business logic (memory, skill, retrieval, propagation, ACL)
│   ├── store/            # ContextStore (URI routing: read/write/ls/stat)
│   ├── retrieval/        # Search strategies (vector, keyword, BM25 rerank)
│   ├── propagation/      # Change propagation rules (skill_dep, table_schema, derived_from)
│   ├── generation/       # L0/L1 content generation
│   ├── llm/              # Embedding client abstraction (OpenAI, NoOp)
│   └── connectors/       # Catalog connectors (mock for MVP)
├── sdk/                  # Python SDK (typed HTTP client)
├── plugins/openclaw/     # OpenClaw context-engine plugin
├── bridge/               # TS bridge + Python sidecar (OpenClaw ↔ ContextHub)
├── alembic/              # Database migrations
├── tests/                # Integration tests (visibility, propagation, retrieval, etc.)
├── plan/                 # Design documents (15 files, from invariants to implementation plan)
└── docs/                 # Setup guides, verification plans, integration guides
```

### Key Modules

| Module | Responsibility |
|--------|---------------|
| `api/` | HTTP layer: route handlers, tenant-scoped middleware (`X-Account-Id` → `SET LOCAL`), dependency injection |
| `db/` | `PgRepository` (raw asyncpg connection pool) + `ScopedRepo` (request-scoped executor with `app.account_id` set) |
| `store/` | `ContextStore` — the `ctx://` URI router. Maps `read/write/ls/stat` operations to PostgreSQL queries |
| `services/memory_service.py` | Add, list, promote memories with `derived_from` lineage tracking |
| `services/skill_service.py` | Publish versions, subscribe, resolve `pinned`/`latest`/explicit version |
| `retrieval/` | `VectorStrategy` (pgvector), `KeywordStrategy` (ILIKE fallback), `BM25Reranker`, ACL filtering |
| `propagation/` | Outbox drain loop, three-tier rule dispatch, retry with exponential backoff, NOTIFY + sweep |
| `services/acl_service.py` | Default visibility (team hierarchy via recursive CTE) + write permission checks |
| `generation/` | L0 summary + L1 structured overview generation (via LLM or template) |
| `connectors/` | `CatalogConnector` interface + `MockCatalogConnector` for MVP |

### Bridge Architecture (OpenClaw Integration)

```
bridge/
├── openclaw.plugin.json     # Plugin manifest (kind: "context-engine", slot: exclusive)
├── package.json             # npm package with openclaw.extensions entry
├── src/
│   ├── index.ts             # Plugin entry: register(api) → registerContextEngine + registerTool
│   ├── bridge.ts            # ContextHubBridge: TS ContextEngine → HTTP calls to sidecar
│   ├── tools.ts             # 7 tool definitions (ls/read/grep/stat/store/promote/publish)
│   └── sidecar.py           # Python FastAPI wrapper: HTTP → ContextHubContextEngine → SDK
└── dist/                    # Compiled JS output
```

The bridge uses a **two-process architecture**: the TS bridge runs inside the OpenClaw Node.js gateway, forwarding context engine calls via HTTP to a Python sidecar. The sidecar hosts the actual `ContextHubContextEngine` plugin, which uses the Python SDK to communicate with the ContextHub server. This design avoids embedding Python in Node.js while keeping the plugin interface clean.
