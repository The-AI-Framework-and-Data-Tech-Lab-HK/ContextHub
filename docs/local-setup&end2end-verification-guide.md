# ContextHub MVP — Local Setup & End-to-End Verification Guide

> No Docker required. Uses Homebrew-installed PostgreSQL + pgvector on macOS.

## Prerequisites

- **macOS** with [Homebrew](https://brew.sh) installed
- **Python 3.12+**
- **Node.js 18+** (only needed for Bridge / Part B verification)
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

> **Note**: `SUPERUSER` is required because the schema uses `FORCE ROW LEVEL SECURITY`.
> This is fine for local development.

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

# OpenClaw Plugin
pip install -e plugins/openclaw/
```

---

## 4. Run Database Migrations

```bash
alembic upgrade head
```

Expected output:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema...
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, Force row level security
```

This creates all tables and inserts seed data (teams + agent memberships).

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
  - Promoted to team: ctx://team/engineering/shared_knowledge/...
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

All non-integration tests run. Integration tests are automatically skipped.

### 7b. DB-Backed Integration Tests (requires running PostgreSQL)

```bash
CONTEXTHUB_INTEGRATION=1 pytest \
  tests/test_integration_propagation.py \
  tests/test_integration_collaboration.py \
  tests/test_integration_visibility.py \
  tests/test_datalake.py \
  -v
```

---

## 8. Bridge Verification (Part B)

### 8a. TypeScript Compilation

```bash
cd bridge
npm install
npx tsc --noEmit   # type-check only
npx tsc            # full build → dist/
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
# Expected: tool definitions list

curl -X POST http://localhost:9100/assemble \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "test", "messages": [], "tokenBudget": 4000}'
# Expected: JSON with systemPromptAddition
```

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
| 6 | Unit tests pass | `pytest tests/ -v` | All PASSED |
| 7 | Integration tests pass | `CONTEXTHUB_INTEGRATION=1 pytest ...` | All PASSED |
| 8 | E2E demo completes | `python scripts/demo_e2e.py` | 7 steps succeed |
| 9 | TS bridge compiles | `cd bridge && npx tsc` | No errors |
| 10 | Sidecar starts | `python bridge/src/sidecar.py` | Listening on :9100 |
| 11 | Sidecar health | `curl localhost:9100/health` | `{"status":"ok"}` |

---

## Troubleshooting

### `unrecognized configuration parameter "app.account_id"`

The `contexthub` database user is not a superuser. Fix:

```bash
psql postgres -c "ALTER USER contexthub WITH SUPERUSER;"
```

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
