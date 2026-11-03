# ContextHub MVP — Local Setup & End-to-End Verification Guide

> No Docker required. Uses Homebrew-installed PostgreSQL + pgvector on macOS.
>
> This sets up the shared ContextHub server. Choose the [OpenClaw integration](openclaw-integration-guide.md) or the [Deep Agents tutorial](deepagents-integration-guide.md) for the agent runtime; Deep Agents does not require a bridge or sidecar.

## Prerequisites

- **macOS** with [Homebrew](https://brew.sh) installed
- **Python 3.12+**
- **Node.js** (only for OpenClaw; see its [setup prerequisites](openclaw-integration-guide.md#prerequisites))
- **Conda** should be deactivated before running project commands

---

## 1. Install PostgreSQL + pgvector via Homebrew

```bash
brew install postgresql@16
brew install pgvector
```

Start the PostgreSQL service:

```bash
brew services start postgresql@16
```

Verify it's running:

```bash
pg_isready
# Expected: "accepting connections"
```

---

## 2. Create Database and User

```bash
psql postgres
```

Inside the `psql` shell, run:

```sql
CREATE USER contexthub WITH PASSWORD 'contexthub' SUPERUSER;
CREATE DATABASE contexthub OWNER contexthub;
\c contexthub
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
\q
```

> This local demo account has database administrator privileges, so PostgreSQL does not restrict it to one tenant's rows. It cannot validate database-level tenant isolation. `FORCE ROW LEVEL SECURITY` does not require superuser access; the [Deep Agents tutorial](deepagents-integration-guide.md) demonstrates an ordinary application account and explains the current background-worker limitation.

Verify the connection:

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "SELECT 1"
```

---

## 3. Set Up the Python Environment

**Important**: Deactivate conda first to avoid PATH conflicts:

```bash
conda deactivate
```

Then create and activate the project venv:

```bash
cd /path/to/ContextHub

python3 -m venv .venv
source .venv/bin/activate
```

Install all dependencies:

```bash
# Core project (with dev extras)
pip install -e ".[dev]"

# greenlet is required by SQLAlchemy's async engine (used by Alembic)
pip install greenlet

# SDK
pip install -e sdk/
```

Install the adapter you will use:

```bash
# OpenClaw
pip install -e ./plugins/openclaw

# Deep Agents (plus the provider package for your model)
pip install -e ./plugins/deepagents langchain-openai
```

---

## 4. Run Database Migrations

```bash
alembic upgrade head
```

This applies all migrations and inserts seed data (teams + agent memberships). Run `alembic current` afterward and check that it reports the migration head; the migration list grows as the schema evolves.

---

## 5. Start the ContextHub Server

**Terminal 1** (keep this running):

```bash
source .venv/bin/activate
uvicorn contexthub.main:app --port 8000
```

Expected:

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Verify in another terminal:

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok"}
```

You can also open http://localhost:8000/docs in a browser to see all API routes.

> **Note**: If `OPENAI_API_KEY` is not set in `.env`, the server uses a NoOp embedding
> client. Embedding-dependent features (vector search, sql-context) will return limited
> results. This is expected for local MVP verification.

---

## 6. Run the End-to-End Demo

**Terminal 2**:

```bash
cd /path/to/ContextHub
source .venv/bin/activate
python scripts/demo_e2e.py
```

The demo script automatically ensures `query-agent` has write access to the `engineering`
team. It walks through 7 steps:

| Step | Description |
|------|-------------|
| 1 | `query-agent` writes a private memory |
| 2 | `query-agent` creates and publishes Skill v1 |
| 3 | `query-agent` promotes memory to `team/engineering` |
| 4 | `analysis-agent` retrieves shared memory and subscribes to Skill |
| 5 | `query-agent` publishes breaking Skill v2 |
| 6 | Verify propagation: stale/advisory detection |
| 7 | (Vertical) Catalog sync + sql-context query |

Expected final output:

```
============================================================
  MVP Demo Complete
============================================================
  - Private memory created: ctx://agent/query-agent/memories/...
  - Promoted to team: ctx://team/engineering/memories/shared_knowledge/...
  - Skill v1 + v2 published, breaking propagation triggered
  - Cross-agent visibility verified
  - Catalog sync + sql-context demonstrated
```

---

## 7. Run Tests

### 7a. Fast Unit Tests (no database required)

```bash
pytest tests/ -v
```

Tests depending on database fixtures are skipped unless explicitly enabled. The optional Deep Agents suite is not collected by this command; run it as shown below. Some research tests also require locally generated artifacts that are not shipped in Git; consult a failure's missing-file path before treating it as an adapter regression.

### 7b. DB-Backed Integration Tests (requires running PostgreSQL)

Use a disposable database. The current test fixtures connect to the `contexthub` database on localhost:5432 with fixed test credentials and truncate business tables; setting `DATABASE_URL` does not redirect those fixtures. Do not enable these tests against an existing research database.

```bash
CONTEXTHUB_INTEGRATION=1 pytest \
  tests/test_integration_propagation.py \
  tests/test_integration_collaboration.py \
  tests/test_integration_visibility.py \
  tests/test_datalake.py \
  -v
```

---

## 8. Agent Adapter Verification

Steps 8a–8b are for OpenClaw. For Deep Agents, go to 8c.

### 8a. TypeScript Compilation

```bash
cd bridge
npm ci
npm test          # builds dist/ and checks tool registration/forwarding
cd ..
```

Expected: no errors; `bridge/dist/` contains `index.js`, `bridge.js`, and `.d.ts` files.

### 8b. Python Sidecar

With the ContextHub server running (Terminal 1):

```bash
python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000
```

Verify:

```bash
curl http://localhost:9100/health
# Expected: {"status":"ok"}

curl http://localhost:9100/info
# Expected: ContextEngine info JSON

curl http://localhost:9100/tools
# Expected: 8 tools, all named contexthub_...

curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -d '{"name": "contexthub_ls", "args": {"path": "ctx://"}}'

curl -X POST http://localhost:9100/assemble \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "test", "messages": [], "tokenBudget": 4000}'
# Expected: JSON with systemPromptAddition
```

### 8c. Deep Agents

```bash
python -m pip install -e './plugins/deepagents[dev]'
python -m pytest plugins/deepagents/tests
```

These tests use mocked HTTP responses and a scripted model. For a real server and model, follow the [Deep Agents tutorial](deepagents-integration-guide.md), including its optional live acceptance check. All eight OpenClaw operation names are also available in Deep Agents, which adds two document-section tools. The [developer guide](../design%20and%20development/development-guide.md#agent-integrations) compares the integrations.

---

## 9. Manual API Verification (Optional)

```bash
# Trigger full catalog sync
curl -X POST http://localhost:8000/api/v1/datalake/sync \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent" \
  -H "Content-Type: application/json" \
  -d '{"catalog": "mock"}'

# List synced tables
curl http://localhost:8000/api/v1/datalake/mock/prod \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# Get full context for a single table
curl http://localhost:8000/api/v1/datalake/mock/prod/orders \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# Query table lineage
curl http://localhost:8000/api/v1/datalake/mock/prod/orders/lineage \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# SQL context assembly
curl -X POST http://localhost:8000/api/v1/search/sql-context \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent" \
  -H "Content-Type: application/json" \
  -d '{"query": "How many orders per user?", "catalog": "mock", "top_k": 3}'
```

---

## Resetting the Database

If you need a clean start (e.g., to re-run the demo from scratch):

```bash
# Option A: Truncate business data only (keeps seed data)
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  TRUNCATE contexts, dependencies, change_events,
           table_metadata, lineage, table_relationships,
           query_templates, skill_versions, skill_subscriptions
  CASCADE;
"

# Option B: Full database reset (re-runs migrations + seed data)
psql postgres -c "DROP DATABASE IF EXISTS contexthub;"
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
alembic upgrade head
```

---

## Stopping / Restarting PostgreSQL

```bash
brew services stop postgresql@16     # stop
brew services start postgresql@16    # start
brew services restart postgresql@16  # restart
```

---

## Verification Checklist

| # | Item | Command / Method | Expected |
|---|------|-----------------|----------|
| 1 | PostgreSQL running | `pg_isready` | accepting connections |
| 2 | Migrations applied | `alembic upgrade head` | No errors |
| 3 | Server started | `uvicorn contexthub.main:app --port 8000` | Listening on :8000 |
| 4 | Health check | `curl localhost:8000/health` | `{"status":"ok"}` |
| 5 | API docs visible | Browser → `localhost:8000/docs` | Datalake routes present |
| 6 | Unit tests | `pytest tests/ -v` | DB tests skipped; investigate failures and missing research artifacts |
| 7 | Integration tests pass | `CONTEXTHUB_INTEGRATION=1 pytest ...` | All PASSED |
| 8 | E2E demo completes | `python scripts/demo_e2e.py` | 7 steps succeed |
| 9 | TS bridge compiles | `cd bridge && npx tsc` | No errors |
| 10 | Sidecar starts | `python bridge/src/sidecar.py` | Listening on :9100 |
| 11 | Sidecar health | `curl localhost:9100/health` | `{"status":"ok"}` |
| 12 | Deep Agents tools | `python -m pytest plugins/deepagents/tests` | All PASSED after installing adapter test dependencies |

---

## Troubleshooting

### `unrecognized configuration parameter "app.account_id"`

The failing database connection has not set the tenant context used by row-level security. Request-scoped queries should bind `app.account_id` through `repo.session(account_id)`. The current propagation worker has an unbound dependency read; for an adapter-only demo with an ordinary database account, disable propagation and lifecycle sweeps as shown in the [Deep Agents tutorial](deepagents-integration-guide.md#3-configure-and-start-contexthub--terminal-1). Granting superuser privileges bypasses the isolation check rather than fixing tenant binding.

### `No module named 'greenlet'`

```bash
pip install greenlet
```

### Propagation errors on server startup (`Failed to update embedding`)

Leftover `change_events` from a previous run are being processed without an embedding
client. Clear them:

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  UPDATE change_events
  SET delivery_status = 'processed', processed_at = NOW()
  WHERE delivery_status IN ('pending', 'retry', 'processing');
"
```

Or disable the propagation engine in `.env`:

```
PROPAGATION_ENABLED=false
```

### Demo Step 2 returns 403 Forbidden

The `query-agent` lacks direct membership in the `engineering` team. The updated demo
script handles this automatically. If running an older version of the script:

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
  VALUES ('query-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE)
  ON CONFLICT DO NOTHING;
"
```

### `alembic` or `uvicorn` picks up conda instead of .venv

Always deactivate conda first:

```bash
conda deactivate
source .venv/bin/activate
```

Verify with `which alembic` — it should point to `.venv/bin/alembic`, not
`/opt/anaconda3/bin/alembic`.

