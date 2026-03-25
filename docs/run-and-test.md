# Run & Test Guide

This document records how to run AMC in development and production-like modes.

## 1) Prerequisites

- Python `>=3.11`
- Repo root: `ContextHub`
- Config files:
  - `config/config.yaml`
  - `.env` (copy from `.env.example`)

Optional external services:
- Neo4j service (for graph backend in later phases)
- PostgreSQL + pgvector extension (default vector backend)

## 2) Install Dependencies

### Option A: `uv` (recommended)

```bash
cd /path/to/ContextHub
uv sync --extra dev
```

### Option B: `pip` + virtualenv

```bash
cd /path/to/ContextHub
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

## 2.1) Prepare pgvector (default vector DB)

AMC now defaults to `pgvector` for vector storage.

1) Ensure PostgreSQL is running.
2) Create/verify extension:

```bash
psql "postgresql://<user>:<password>@127.0.0.1:5432/postgres" -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

3) Configure `.env`:

```bash
AMC_VECTOR_STORE_BACKEND=pgvector
AMC_PGVECTOR_DSN=postgresql://<user>:<password>@127.0.0.1:5432/postgres
AMC_PGVECTOR_SCHEMA=public
AMC_PGVECTOR_TABLE=amc_trajectory_index
```

Notes:
- If `AMC_PGVECTOR_DSN` is empty while backend is `pgvector`, vector indexing is auto-disabled.
- You can still switch back to Chroma via `AMC_VECTOR_STORE_BACKEND=chroma`.

## 3) Run Tests

From repo root:

```bash
# Full test suite
pytest src/tests

# Unit only (fast, default for local iteration)
pytest src/tests -m "not integration"

# Integration only
pytest src/tests -m integration

# Milestone M1 focused tests
pytest src/tests -m m1
```

Notes:
- Some integration tests are intentionally skipped until Neo4j/Chroma wiring is fully enabled.
- If your shell does not auto-select `.venv`, use `.venv/bin/pytest ...`.

## 4) Run Project in Command Line (Foreground)

### Quick start with `uvicorn`

```bash
cd /path/to/ContextHub
source .venv/bin/activate
uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000 --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

Example commit endpoint:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/amc/commit" \
  -H "Content-Type: application/json" \
  -d @sample_request.json
```

### Commit via CLI script (no HTTP server needed)

This command reads a trajectory JSON file and runs Phase 1 commit pipeline
directly in-process. It returns where L0/L1 and graph artifacts are stored.

```bash
cd /path/to/ContextHub
source .venv/bin/activate
amc-commit-trajectory sample_traj/traj1.json --pretty
```

Example output fields:
- `storage.base_path`
- `storage.l0_abstract_path`
- `storage.l1_overview_path`
- `storage.raw_graph_path`
- `storage.clean_graph_path`

You can also run module style:

```bash
python -m cli.commit_trajectory sample_traj/traj1.json --pretty
```

Enable graph PNG visualization (default is off):

```bash
amc-commit-trajectory sample_traj/traj1.json --visualize-graph-png --pretty
```

This writes extra files in the same trajectory directory:
- `raw_graph.png`
- `clean_graph.png`

Note:
- PNG visualization requires `matplotlib`.
- Node labels are `tool_name`; edge labels are intentionally hidden.

Configure LLM-based dataflow extraction (CLI/API share same settings):

```bash
export AMC_COMMIT_DATAFLOW_EXTRACTOR=llm
export AMC_LLM_MODEL=gpt-4.1-mini
export AMC_COMMIT_DATAFLOW_LLM_TEMPERATURE=0.0
export AMC_OPENAI_API_KEY=<your-key>
```

If key is missing, runtime falls back to `rule_based`.

Force-disable idempotency for one CLI run (useful only when config/env enabled idempotency):

```bash
amc-commit-trajectory sample_traj/traj2.json --disable-idempotency --pretty
```

Verify Neo4j graph write (commit + query labels/edge types in one command):

```bash
amc-verify-neo4j sample_traj/traj1.json --disable-idempotency --pretty
```

Useful option:
- `--force-rule-based`: temporarily force `AMC_COMMIT_DATAFLOW_EXTRACTOR=rule_based` for faster verification.

Verify pgvector rows after one commit:

```bash
psql "$AMC_PGVECTOR_DSN" -c "SELECT id, metadata->>'uri' AS uri FROM public.amc_trajectory_index LIMIT 5;"
```

## 5) Run Project as a Service (systemd)

Use this for Linux server deployment.

### 5.1 Create service file

Create `/etc/systemd/system/amc.service`:

```ini
[Unit]
Description=AMC API Service
After=network.target

[Service]
Type=simple
User=<YOUR_USER>
WorkingDirectory=/path/to/ContextHub
EnvironmentFile=/path/to/ContextHub/.env
ExecStart=/path/to/ContextHub/.venv/bin/uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 5.2 Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable amc
sudo systemctl start amc
```

### 5.3 Check status/logs

```bash
sudo systemctl status amc
journalctl -u amc -f
```

### 5.4 Restart after code/config updates

```bash
sudo systemctl restart amc
```

## 6) Common Troubleshooting

- `ModuleNotFoundError`: ensure startup command uses `--app-dir src`.
- Wrong config values: check `.env` and `AMC_CONFIG_PATH` (if set).
- Permission errors (logs/data): ensure write access to paths in:
  - `storage.content_store.localfs_root`
  - `audit.file_path`
- Neo4j connection errors: verify `AMC_NEO4J_URI/USER/PASSWORD` and service status.
- pgvector connection errors: verify `AMC_PGVECTOR_DSN` and that `CREATE EXTENSION vector` succeeded.

### Repeated commit is skipped (idempotent)

By default, Phase 1 disables idempotency, so repeated commits will update/overwrite storage.

If you want duplicate payloads to be skipped, enable idempotency:

1) In `config/config.yaml`:

```yaml
commit:
  idempotency:
    enabled: true
```

2) Or via env override:

```bash
AMC_COMMIT_IDEMPOTENCY_ENABLED=true
```

Important:
- In shell, `AMC_COMMIT_IDEMPOTENCY_ENABLED=true` alone is **not exported**.
- Use one of:
  - `export AMC_COMMIT_IDEMPOTENCY_ENABLED=true`
  - `AMC_COMMIT_IDEMPOTENCY_ENABLED=true amc-commit-trajectory ...`

## 7) Current Phase 1 Behavior for L0/L1 + Dataflow

In current Phase 1:
- L0/L1 summaries are generated by LLM when `AMC_OPENAI_API_KEY` is configured (via `src/core/commit/summary_llm.py`);
  if LLM call fails or key is missing, it falls back to rule-based summary in `src/core/commit/summarizer.py`.
- Dataflow edge extraction can run in:
  - `rule_based` mode (legacy)
  - `llm` mode (`commit.graph.dataflow_extractor=llm`) with two key rules:
    1) echoed input/output overlap is not treated as new output evidence;
    2) partial token inclusion (e.g. enum output inside later SQL command) counts as dependency.

