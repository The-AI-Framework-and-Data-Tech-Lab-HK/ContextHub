# ContextHub

**Unified context governance middleware for enterprise multi-agent systems.**

English | [中文](README_zh.md)

## The Problem: From Memory Management to Context Governance

When multiple AI agents collaborate on the same business entities in an enterprise environment, their contexts — memories, skills, policy documents, schemas — are siloed, unversioned, and disconnected. Research shows **79% of multi-agent failures stem from coordination problems, not technical bugs** ([Zylos Research, 2026](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical)), and **36.9% of failures come from inter-agent misalignment** — agents ignoring, duplicating, or contradicting each other's work ([Cemri et al., 2025](https://arxiv.org/abs/2503.13657)). These failures cannot be fixed by improving individual model capabilities; they are structural deficits in the system architecture.

Existing frameworks treat "agent context management" as synonymous with **memory management**. Governed Memory, Collaborative Memory, MemOS — all center on memory as the core abstraction. But enterprise agent systems need to govern far more than memory:

| Context Type | Examples | Multi-version Governance Coverage in Literature |
|---|---|---|
| **Memory** | Conversation history, entity state, working memory | Relatively most covered; multi-user collaborative versioning still scarce |
| **Skill** | Tool definitions, prompt templates, agent configs | **Near-blank** — no end-to-end lifecycle for breaking change detection + subscriber notification |
| **Resource (RAG docs)** | Policy documents, compliance rules, knowledge bases | Only "retrieve the latest version," not "propagate changes along dependency graphs" |
| **Structured Metadata** | Database schemas, data lake catalogs | No research in AI agent context |

ContextHub addresses this gap. To our knowledge, **unified version governance** of Memory, Skill, Resource, and Structured Metadata — as an end-to-end problem — has no systematic treatment in existing literature.

## Design Contributions

ContextHub is enterprise context governance middleware that provides a **unified context state layer** for multi-agent collaboration — encompassing shared memory, visibility boundaries, version governance, and change propagation.

| Contribution | What It Solves | Why It's New |
|---|---|---|
| **Skill version management + breaking change propagation** | Publisher marks `is_breaking` → subscribers get `stale` / `advisory` notifications → pinned subscribers remain stable | No existing AI agent framework handles the full lifecycle: publish → breaking flag → subscriber notification → stale marking → recovery |
| **Dependency-graph-driven change propagation** | When an upstream policy/schema changes, all downstream agents that depend on it are automatically notified or updated | Temporal/Corrective RAG solves "retrieve the current doc" but not "who depends on this doc and needs to be notified" |
| **Hierarchical team ownership with visibility inheritance** | Child teams see parent team content; parent teams don't see child team private content by default | Goes beyond flat user/agent/app isolation (Mem0) to enterprise org structures |
| **L0/L1/L2 layered retrieval model** | One-line summary (L0, vector search) → structured overview (L1, rerank) → full content (L2, on-demand) | Reduces context token consumption by 60-80% vs. flat schema dumps |
| **PostgreSQL-centric single-DB architecture** | ACID transactions, RLS tenant isolation, LISTEN/NOTIFY for change propagation, recursive CTEs for lineage, pgvector for semantic search — all in one database | Eliminates dual-write consistency problems between separate vector stores, message queues, and metadata databases |

### How It Differs from Existing Solutions

| Framework | Limitation | ContextHub's Answer |
|---|---|---|
| **Mem0** | Flat user/agent/app isolation; no team hierarchy, no change propagation, no versioning; SaaS-only | Hierarchical teams + propagation + versions + self-hosted |
| **CrewAI / LangGraph** | Memory systems scoped to a single framework; can't manage cross-framework, cross-team, cross-time organizational knowledge | Framework-agnostic middleware via SDK + plugin |
| **OpenAI Agents SDK** | No built-in memory, no ACL, no tenant isolation | Full governance layer |
| **Governed Memory (Personize.ai)** | Closest approach but focused on CRM entities (contacts/companies/deals), not general agent context | General-purpose `ctx://` URI abstraction for any context type |
| **OpenViking** | Core context management concepts (everything-is-a-file + memory pipeline + vector search) but personal-edition only — no multi-agent isolation, team hierarchy, ACL, or change propagation | Inherits OpenViking's URI + L0/L1/L2 abstractions; extends to enterprise multi-tenant architecture |

## Architecture

```
         Agents (via OpenClaw Plugin / SDK)
              │
              ▼
    ContextHub Server (FastAPI)
    ├── ContextStore       — ctx:// URI routing (read/write/ls/stat)
    ├── MemoryService      — promote, derived_from, team sharing
    ├── SkillService       — publish, subscribe, version resolution
    ├── RetrievalService   — unified search (pgvector + BM25 rerank)
    ├── PropagationEngine  — outbox drain, retry, dependency/subscription dispatch
    └── ACLService         — default visibility / write permissions
              │
              ▼
    PostgreSQL + pgvector
    (metadata, content, vectors, events — all in one DB)
```

**Single database. No external vector store. No message queue.** PostgreSQL handles ACID transactions, RLS tenant isolation, LISTEN/NOTIFY for change propagation, recursive CTEs for lineage queries, and pgvector for semantic search. This deliberate choice eliminates dual-write consistency problems and minimizes infrastructure complexity for on-premise enterprise deployment.

### Design Principles

- **URI is a logical address, not a physical path.** `ctx://datalake/prod/orders` maps to a row in PostgreSQL, not a file on disk. Agents perceive file semantics; the system provides database guarantees.
- **Metadata and content co-located.** L0/L1/L2 content lives in PostgreSQL TEXT columns (TOAST handles large text), updated atomically with metadata in the same transaction.
- **Only L0 is vectorized.** L0 summaries (~100 tokens) are embedded for semantic search. L1/L2 are retrieved by URI from the same table — no cross-system overhead.

## Core Capabilities

### Multi-Agent Collaboration
- **Team ownership model** with hierarchical visibility inheritance (child reads parent; parent does not see child by default)
- **Memory promotion** from `private → team → organization` scope, with `derived_from` lineage tracking
- **Cross-agent knowledge reuse** — promoted memories are searchable by teammates

### Skill Version Management
- Publish new versions with `is_breaking` flag
- Subscribers choose `pinned` (stable) or `latest` (floating) resolution strategy
- Breaking changes mark downstream dependents as `stale` with advisory notifications
- Published versions are immutable; URI always returns latest published (pin is a perspective, not a new address)

### Change Propagation
- Three-tier propagation rules: pure rule (70%, zero tokens) / template substitution (20%) / LLM reasoning (10%)
- Outbox pattern with `change_events` table as sole source of truth
- NOTIFY for fast wake-up + periodic sweep for guaranteed delivery
- Automatic retry with exponential backoff; crash recovery via lease timeout
- Idempotent side effects: `mark_stale`, `auto_update`, `notify`, `advisory`

### L0/L1/L2 Layered Retrieval
- **L0**: one-line summary + embedding (vector search via pgvector)
- **L1**: structured overview (BM25 keyword rerank)
- **L2**: full content (on-demand loading)
- Graceful degradation: when embedding service is unavailable, falls back to keyword search

### Visibility & Tenant Isolation
- Row-Level Security (RLS) on all agent-facing tables
- `SET LOCAL app.account_id` scoped to each transaction via request-scoped `ScopedRepo`
- Default visibility based on team hierarchy + scope rules; explicit ACL as post-MVP overlay

## Usage

ContextHub is designed as a **context engine** for AI agent runtimes. The primary integration is with [OpenClaw](https://github.com/anthropics/openclaw) — ContextHub replaces OpenClaw's built-in context engine, providing enterprise-grade context governance to every agent session.

### As OpenClaw Context Engine

Install ContextHub as an OpenClaw context engine plugin:

```bash
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

Once installed, ContextHub works transparently with every OpenClaw session:

```
User ──► OpenClaw TUI ──► Gateway ──► ContextHub Bridge (TS)
                                        └─► Python Sidecar (:9100)
                                             └─► ContextHub Server (:8000)
                                                  └─► PostgreSQL + pgvector
```

**Automatic behaviors — no agent code changes needed:**

| Event | What ContextHub Does |
|-------|---------------------|
| Agent receives a prompt | `assemble()` searches all visible contexts (memories, skills, schemas) and injects relevant ones into the system prompt |
| Agent completes a response | `afterTurn()` extracts reusable facts from the response and stores them as private memories |

**Agent tools — available in every session:**

| Tool | Description |
|------|-------------|
| `ls` | List contexts under a `ctx://` path |
| `read` | Read context content (skills auto-resolve via version logic) |
| `grep` | Search context content by keyword |
| `stat` | Get metadata for a context entry |
| `contexthub_store` | Store a new private memory |
| `contexthub_promote` | Promote a memory from private → team scope |
| `contexthub_skill_publish` | Publish a new skill version |

### Multi-Agent Collaboration in Action

Two agents in different departments — sharing knowledge through ContextHub with zero manual handoff:

```
Org Structure:
  engineering/
    └── engineering/backend    ← query-agent (backend engineer)
  data/
    └── data/analytics         ← analysis-agent (data analyst, also engineering member)
```

**Scenario: cross-department knowledge reuse**

```
1. query-agent stores a SQL pattern as private memory:
   "JOIN orders and products, GROUP BY month for monthly sales"

2. query-agent promotes this memory to the engineering team:
   → ctx://team/engineering/shared_knowledge/monthly-sales-pattern

3. analysis-agent asks: "How should I query monthly sales?"
   → ContextHub auto-recalls the promoted pattern (via assemble)
   → analysis-agent receives the knowledge — no manual sharing needed

4. query-agent publishes a breaking Skill v2 (sql-generator):
   → analysis-agent (pinned to v1) continues using v1 stably
   → advisory: "v2 available with breaking changes"
   → analysis-agent upgrades at their own pace
```

**What makes this different from a shared document?** ContextHub enforces visibility boundaries (private stays private unless explicitly promoted), tracks lineage (`derived_from`), and propagates changes through dependency graphs — not just "latest version wins."

For the full OpenClaw integration setup (5-terminal stack), see the [OpenClaw Integration Guide](docs/openclaw-integration-guide.md).

### Using the Python SDK

For direct programmatic access without OpenClaw:

```python
from contexthub_sdk import ContextHubClient

client = ContextHubClient(base_url="http://localhost:8000", api_key="...")

# Semantic search across all visible contexts
results = await client.search("monthly sales summary", scope=["datalake"], top_k=5)

# Record a successful case as private memory
memory = await client.add_memory(content="SELECT ... GROUP BY month", tags=["sql", "sales"])

# Promote to team-shared memory
promoted = await client.promote_memory(uri=memory.uri, target_team="engineering/backend")

# Publish a new skill version
version = await client.publish_skill_version(
    skill_uri="ctx://team/engineering/skills/sql-generator",
    content="...",
    changelog="Added window function support",
    is_breaking=True,
)
```

## Roadmap

- [x] **Phase 1 — MVP Core** (complete)
  - Project scaffolding, Docker, PostgreSQL + pgvector setup
  - Core tables with RLS, triggers, seed data
  - Request-scoped DB execution model (`PgRepository` / `ScopedRepo`)
  - ACLService (default visibility / write permissions with recursive CTE team hierarchy)
  - ContextStore (`ctx://` URI routing: read/write/ls/stat)
  - MemoryService (add, list, promote with `derived_from` lineage)
  - SkillService (publish, subscribe, pinned/latest/explicit version resolution)
  - RetrievalService (pgvector search + BM25 rerank + ACL filtering + graceful degradation)
  - PropagationEngine (outbox drain, three-tier rules, retry/recovery, NOTIFY + sweep)
  - Python SDK + OpenClaw context-engine plugin
  - Data lake carrier (MockCatalogConnector, CatalogSyncService, sql-context assembly)
  - Tier 3 integration tests (propagation P-1~P-8, collaboration C-1~C-5, visibility A-1~A-4)
- [ ] **Phase 2 — Explicit ACL & Audit**
  - Explicit ACL allow/deny/field mask overlay on default visibility
  - Audit logging (append-only `audit_log` table)
  - Narrow-scope cross-team sharing via "reference + ACL"
- [ ] **Phase 3 — Feedback & Lifecycle**
  - Feedback loop (adopted/ignored signals, quality scoring)
  - Lifecycle management (automatic stale → archived → deleted transitions)
  - Long document retrieval extensions
- [ ] **Phase 4 — Quantitative Evaluation (ECMB)**
  - Tier 1 benchmarks: SQL Execution Accuracy, Table Retrieval Precision/Recall, Token per Query
  - Tier 2 A/B experiments: L0/L1/L2 vs. flat RAG, with/without structured relations, with/without propagation
- [ ] **Phase 5 — Production Hardening**
  - Multi-instance deployment (`SELECT FOR UPDATE SKIP LOCKED`)
  - MCP Server integration
  - Real catalog connectors (Hive/Iceberg/Delta)
  - Run snapshot / context bundle

## Documentation

| Document | Description |
|----------|-------------|
| [OpenClaw Integration Guide](docs/openclaw-integration-guide.md) | Full setup for running ContextHub as OpenClaw's context engine |
| [Local Setup & E2E Verification](docs/local-setup&end2end-verification-guide.md) | Development environment, database migrations, E2E demo |
| [MVP Verification Plan](docs/mvp-verification-plan.md) | Three-layer verification: automated tests → API demo → runtime contract |
| [Developer Guide](docs/development-guide.md) | API overview, tech stack, project structure |

### Design Documents

The `plan/` directory contains 15 design documents covering the full system design:

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

## References

- [AI Agent Memory Architectures for Multi-Agent Systems](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical) — Zylos Research, 2026
- [How to Design Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026
- [Governed Memory: A Production Architecture for Multi-Agent Workflows](https://arxiv.org/abs/2603.17787) — Taheri, 2026
- [Collaborative Memory: Multi-User Memory Sharing with Dynamic Access Control](https://arxiv.org/abs/2505.18279)
- [OpenViking](https://github.com/volcengine/OpenViking) — Core design inspiration (personal-edition context management)
- [Model Context Protocol (MCP)](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024

## License

[Apache License 2.0](LICENSE)
