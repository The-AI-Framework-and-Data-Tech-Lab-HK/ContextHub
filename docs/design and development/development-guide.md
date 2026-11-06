# ContextHub — Developer Guide

> API reference, tech stack, SDK usage, and project structure for ContextHub contributors.
>
> For the current document-retrieval flow, decisions and evidence, see [Document Retrieval Design](document-retrieval.md).
>
> For local development setup, see [Local Setup & E2E Verification Guide](../setup/local-setup&end2end-verification-guide.md).
> For OpenClaw integration setup, see [OpenClaw Integration Guide](../setup/openclaw-integration-guide.md).
> For Deep Agents, see the [integration tutorial](../setup/deepagents-integration-guide.md) and [adapter reference](../../plugins/deepagents/README.md).

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose (or Homebrew-installed PostgreSQL on macOS)
- PostgreSQL 16 with pgvector

### 1. Clone and install

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
pip install -e ".[dev]" -e ./sdk greenlet
```

### 2. Start PostgreSQL

```bash
docker compose up -d
```

This starts PostgreSQL 16 with pgvector on port 5432 (user: `contexthub`, password: `contexthub`, database: `contexthub`).

For macOS without Docker, see the [Local Setup Guide](../setup/local-setup&end2end-verification-guide.md) for Homebrew-based PostgreSQL installation.

### 3. Run database migrations

```bash
alembic upgrade head
```

### 4. Start the server

```bash
uvicorn contexthub.main:app --reload
```

The API is available at `http://localhost:8000`. OpenAPI docs at `/docs`.

## Python SDK

The same async SDK is used by both agent adapters and can also be called directly. Bind the account and agent identity when constructing the client, and keep it open for the duration of the calls:

```python
import asyncio
from contexthub_sdk import ContextHubClient, Scope

async def main():
    async with ContextHubClient(
        url="http://localhost:8000",
        api_key="changeme",
        account_id="acme",
        agent_id="query-agent",
    ) as client:
        memory = await client.memory.add(
            content="Group sales by month before comparing monthly totals.",
            tags=["sql", "sales"],
        )
        await client.memory.promote(uri=memory.uri, target_team="engineering")
        results = await client.search(
            "monthly sales summary", scope=[Scope.TEAM], top_k=5,
        )
        print(results.model_dump_json(indent=2))

asyncio.run(main())
```

The example uses the seeded `acme` account and `query-agent`; replace `api_key` with the running server's `API_KEY`. Skill version updates use `client.skill.publish(skill_uri=..., content=..., changelog=..., is_breaking=...)` and require an existing skill. Document upload and section reads are under `client.document`.

## Agent integrations

| Aspect | OpenClaw | Deep Agents |
|---|---|---|
| Connection | Context-engine plugin → TypeScript bridge → Python sidecar → SDK | Native async tools → SDK |
| ContextHub tools | 8 tools | The same 8 operation names, plus `contexthub_sections` and `contexthub_read_section` |
| Retrieval and saving | Automatic turn hooks and explicit tool calls | Explicit tool calls; no automatic recall/capture hooks |
| Conversation state | OpenClaw owns history and compaction | Deep Agents owns conversation state and its filesystem |
| Setup | [OpenClaw guide](../setup/openclaw-integration-guide.md) | [Deep Agents tutorial](../setup/deepagents-integration-guide.md) |

Both integrations use `contexthub_ls`, `contexthub_read`, `contexthub_search`, `contexthub_stat`, `contexthub_store`, `contexthub_promote`, `contexthub_skill_publish`, and `contexthub_feedback`. Parameters and framework return formats are defined by each adapter; sharing names does not make their schemas identical. The unprefixed OpenClaw tool names are not supported. Rebuild the bridge and restart OpenClaw and the sidecar after updating, and update custom prompts or tool allowlists to the current names.

Install the optional Deep Agents adapter with `python -m pip install -e ./sdk -e ./plugins/deepagents`, plus the LangChain provider package for your model. Use `create_contexthub_tools(client)` with `create_deep_agent`, then call `ainvoke` or `astream` while the client is open. ContextHub agent identity is bound to the client, not to a conversation thread ID.

## API Overview

All requests require `X-Account-Id`, `X-Agent-Id`, and `X-API-Key` headers for tenant isolation and authentication.

The table lists server HTTP paths. OpenClaw and Deep Agents expose agent tools
with `contexthub_` names; for example, `contexthub_read` calls `/api/v1/tools/read`
and `contexthub_search` calls `/api/v1/search`.

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
| POST | `/api/v1/tools/{ls,read,grep,stat}` | Filesystem-style HTTP helpers; these paths are not agent tool names |
| POST | `/api/v1/documents/ingest` | Upload a document and build its sections |
| GET | `/api/v1/documents/{context_id}/sections` | List document sections |
| GET | `/api/v1/documents/{context_id}/section/{section_id}` | Read a section's source text |
| POST | `/api/v1/feedback` | Report retrieval usefulness |

## How ContextHub Differs from Existing Solutions

| Framework | Limitation | ContextHub's Answer |
|---|---|---|
| **Mem0** | Flat user/agent/app isolation; no team hierarchy, no change propagation, no versioning; SaaS-only | Hierarchical teams + propagation + versions + self-hosted |
| **CrewAI / LangGraph** | Memory systems scoped to a single framework; can't manage cross-framework, cross-team, cross-time organizational knowledge | Framework-agnostic middleware via SDK + plugin |
| **OpenAI Agents SDK** | No built-in memory, no ACL, no tenant isolation | Full governance layer |
| **Governed Memory (Personize.ai)** | Closest approach but focused on CRM entities (contacts/companies/deals), not general agent context | General-purpose `ctx://` URI abstraction for any context type |
| **OpenViking** | Core context management concepts (everything-is-a-file + memory pipeline + vector search) but personal-edition only — no multi-agent isolation, team hierarchy, ACL, or change propagation | Inherits OpenViking's URI + L0/L1/L2 abstractions; extends to enterprise multi-tenant architecture |

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Web Framework | FastAPI | Async, type-safe, auto-generated OpenAPI |
| Database | PostgreSQL 16 | Unified storage for metadata + content + vectors + events |
| Vector Search | pgvector | Same-DB, same-transaction consistency; no dual-write |
| Async Driver | asyncpg | High-performance async PG with native LISTEN/NOTIFY |
| Migrations | Alembic | Schema version management |
| Embedding | Configured provider model and dimension | Context discovery and original-passage vectors; see the document retrieval design for index boundaries |
| HTTP Client | httpx | Lightweight async HTTP for embedding API calls |
| Validation | Pydantic v2 | Request/response models with automatic validation |

## Design Principles

- **URI is a logical address, not a physical path.** `ctx://datalake/prod/orders` maps to a row in PostgreSQL, not a file on disk. Agents perceive file semantics; the system provides database guarantees.
- **Context records are stored in PostgreSQL.** Long documents additionally keep original and extracted files under `DOC_STORE_ROOT`; their section metadata and source ranges are in PostgreSQL. See [document section generation](../setup/document-section-generation.md).
- **Original evidence is searchable without an L0 match.** Context discovery embeds `L0 or L1 or L2` for context discovery and stores separate original-passage vectors for file-backed documents. Source BM25 also searches originals directly. L0/L1 stay at document level; [Document Retrieval Design](document-retrieval.md) explains both routes, the two fusion stages, and their validation limits.

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
├── plugins/deepagents/   # Native async tools, examples, and adapter tests
├── bridge/               # TS bridge + Python sidecar (OpenClaw ↔ ContextHub)
├── alembic/              # Database migrations
├── tests/                # Core unit and integration tests
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
│   ├── tools.ts             # 8 contexthub_ tool definitions
│   └── sidecar.py           # Python FastAPI wrapper: HTTP → ContextHubContextEngine → SDK
└── dist/                    # Compiled JS output
```

The bridge uses a **two-process architecture**: the TS bridge runs inside the OpenClaw Node.js gateway, forwarding context engine calls via HTTP to a Python sidecar. The sidecar hosts the actual `ContextHubContextEngine` plugin, which uses the Python SDK to communicate with the ContextHub server. This design avoids embedding Python in Node.js while keeping the plugin interface clean.

### Verification

Run `python -m pytest tests/test_plugin.py tests/test_sdk.py` for the OpenClaw Python adapter and SDK, and `npm test` from `bridge/` for TypeScript registration and forwarding. After installing `./plugins/deepagents[dev]`, run `python -m pytest plugins/deepagents/tests` for Deep Agents. These adapter checks use simulated model/server responses; real-model verification is described in the integration guides.

The default core suite skips database-dependent tests. Set `CONTEXTHUB_INTEGRATION=1` only for a disposable database: the shared fixtures use a fixed local connection and truncate business tables. The default suite does not collect the optional Deep Agents package. See [local verification](../setup/local-setup&end2end-verification-guide.md) for commands and environment requirements.

## Design Documents

The local, git-ignored `plan/` directory may contain the original design notes below; it is not included in a fresh checkout. Maintained setup and integration instructions are under `docs/` and the adapter READMEs.

| Document | Topic |
|----------|-------|
| `00a-canonical-invariants` | Authoritative constraints: tenant uniqueness, type system, visibility rules, state machines, version immutability |
| `01-storage-paradigm` | Unified storage: URI routing, PG core tables, pgvector, visibility SQL |
| `02-information-model` | L0/L1/L2 three-layer model, memory classification, hotness scoring |
| `03-datalake-management` | Data lake metadata: L2 structured sub-tables, CatalogConnector, Text-to-SQL context assembly |
| `04-multi-agent-collaboration` | Team ownership, skill versioning, memory promotion |
| `05-access-control-audit` | Two-layer access model (default + explicit ACL), field masking |
| `06-change-propagation` | Event-driven propagation: outbox, three-tier rules, retry |
| `07-feedback-lifecycle` | Feedback loop, quality signals, lifecycle governance |
| `08-architecture` | System architecture, module responsibilities, data flows |
| `09-implementation-plan` | MVP claim, verification matrix, tech stack |

