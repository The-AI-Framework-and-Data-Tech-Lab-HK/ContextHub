# ContextHub + OpenClaw End-to-End Integration Guide

> Step-by-step instructions for running ContextHub as the context engine
> inside an OpenClaw agent runtime — fully local, no Docker required.

## Overview

This guide walks through running the full stack:

```
┌─────────────────────────────────────────────────────────────┐
│  Terminal 1: PostgreSQL         (Homebrew service, always on) │
│  Terminal 2: ContextHub Server  (FastAPI on :8000)            │
│  Terminal 3: Python Sidecar     (FastAPI on :9100)            │
│  Terminal 4: OpenClaw Gateway   (Node.js on :18789)           │
│  Terminal 5: OpenClaw TUI       (interactive agent chat)      │
└─────────────────────────────────────────────────────────────┘
```

The data flow is:

```
OpenClaw TUI (you type) ──► OpenClaw Gateway
  └─ ContextHubBridge (TS, context-engine plugin)
       └─ HTTP ──► Python Sidecar (:9100)
            └─ ContextHubContextEngine (Python plugin)
                 └─ ContextHubClient (SDK)
                      └─ ContextHub Server (:8000)
                           └─ PostgreSQL + pgvector
```

## Prerequisites

| Requirement        | Version     | Install                         |
| ------------------ | ----------- | ------------------------------- |
| macOS with Homebrew| —           | https://brew.sh                 |
| Python             | 3.11+       | `brew install python@3.12`      |
| Node.js            | ≥ 22.12     | `brew install node`             |
| pnpm               | 9+          | `npm install -g pnpm`           |
| PostgreSQL         | 16          | `brew install postgresql@16`    |
| pgvector           | —           | `brew install pgvector`         |

You also need an **LLM API key** (Anthropic or OpenAI) for the OpenClaw agent.

---

## One-time setup

### 1. PostgreSQL + pgvector (one-time)

```bash
brew install postgresql@16
brew install pgvector
brew services start postgresql@16
```

Verify:

```bash
pg_isready
# Expected: "accepting connections"
```

### 2. Create database (one-time)

```bash
psql postgres -c "CREATE USER contexthub WITH PASSWORD 'contexthub' SUPERUSER;"
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
```

### 3. ContextHub Python environment (one-time)

```bash
cd /path/to/ContextHub

# Deactivate conda if active
conda deactivate

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
pip install greenlet
pip install -e sdk/
pip install -e plugins/openclaw/
```

### 4. Run database migrations (one-time, or after schema changes)

```bash
cd /path/to/ContextHub
source .venv/bin/activate
alembic upgrade head
```

### 5. Build the TypeScript bridge (one-time, or after bridge code changes)

```bash
cd /path/to/ContextHub/bridge
npm install
npm run build
```

### 6. Clone and build OpenClaw from source (one-time)

```bash
git clone https://github.com/openclaw/openclaw.git /path/to/public/openclaw
cd /path/to/public/openclaw

# If git SSH fails for some dependencies:
git config url."https://github.com/".insteadOf "git@github.com:"

pnpm install
pnpm ui:build
pnpm build
```

> **Note**: `/path/to/public/openclaw` is the **OpenClaw main repo** (a Node.js
> project with `package.json`). Do **not** confuse it with `ContextHub/plugins/openclaw/`,
> which is a Python plugin package.
>
> **Note**: `pnpm ui:build` is required for OpenClaw **≥ v2026.4.x**. If you are
> on an older version (e.g. v2026.3.14), you can skip it — `pnpm install && pnpm build`
> is sufficient.

### 7. Install the ContextHub plugin into OpenClaw (one-time)

```bash
cd /path/to/public/openclaw
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

Expected output (warnings are OK):

```
Exclusive slot "contextEngine" switched from "legacy" to "contexthub".
Linked plugin path: ~/path/to/ContextHub/bridge
Restart the gateway to load plugins.
```

### 8. Configure OpenClaw LLM provider (one-time)

```bash
cd /path/to/public/openclaw
pnpm openclaw configure
```

Follow the interactive prompts to set your LLM API key (Anthropic / OpenAI).

Or edit `~/.openclaw/openclaw.json` directly:

```json5
{
  "plugins": {
    "slots": {
      "contextEngine": "contexthub"
    },
    "entries": {
      "contexthub": {
        "enabled": true,
        "config": {
          "sidecarUrl": "http://localhost:9100"
        }
      }
    }
  }
}
```

---

## Running the full stack

After the one-time setup, here is how to start everything each time.

### Terminal 1 — PostgreSQL

PostgreSQL runs as a Homebrew service. It starts automatically on boot if
you ran `brew services start postgresql@16`. Verify it's running:

```bash
pg_isready
# Expected: "accepting connections"
```

If not running:

```bash
brew services start postgresql@16
```

### Terminal 2 — ContextHub Server

```bash
cd /path/to/ContextHub
source .venv/bin/activate
uvicorn contexthub.main:app --port 8000
```

Keep this terminal open. Verify in another tab:

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok"}
```

> **Note**: If `OPENAI_API_KEY` is not set in `.env`, the server uses a NoOp
> embedding client. Vector search returns limited results. This is expected
> for local MVP verification.

### Terminal 3 — Python Sidecar

```bash
cd /path/to/ContextHub
source .venv/bin/activate
python bridge/src/sidecar.py \
  --port 9100 \
  --contexthub-url http://localhost:8000 \
  --agent-id query-agent \
  --account-id acme
```

Keep this terminal open. Verify:

```bash
curl http://localhost:9100/health
# Expected: {"status":"ok"}

curl http://localhost:9100/tools
# Expected: JSON array of 7 tool definitions
```

### Terminal 4 — OpenClaw Gateway

```bash
cd /path/to/public/openclaw
pnpm openclaw gateway
```

Keep this terminal open. You should see the gateway start on port 18789.
Look for the ContextHub plugin loading in the logs.

### Terminal 5 — OpenClaw TUI (interactive chat)

```bash
cd /path/to/public/openclaw
pnpm openclaw tui
```

You can now chat with the agent. Behind the scenes:

- **Every prompt**: `assemble()` auto-recalls relevant context from ContextHub
  and injects it into the system prompt.
- **Every response**: `afterTurn()` extracts reusable facts from the assistant's
  reply and writes them as private memories in ContextHub.
- **Agent tools**: The agent can call `ls`, `read`, `grep`, `stat`,
  `contexthub_store`, `contexthub_promote`, and `contexthub_skill_publish`.

---

## Verifying the integration

### Quick check: is the plugin loaded?

```bash
cd /path/to/public/openclaw
pnpm openclaw doctor
```

Look for `contexthub` in the context engine section.

### Quick check: sidecar endpoints

```bash
# Info
curl http://localhost:9100/info

# Simulate assemble (auto-recall)
curl -X POST http://localhost:9100/assemble \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "test", "messages": [{"role": "user", "content": "What is the orders table schema?"}]}'

# Simulate tool dispatch
curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -d '{"name": "ls", "args": {"path": "ctx://"}}'
```

### Full ContextHub E2E demo (without OpenClaw)

This verifies the ContextHub server itself — multi-agent memory, skills,
propagation:

```bash
cd /path/to/ContextHub
source .venv/bin/activate
python scripts/demo_e2e.py
```

---

## Multi-agent testing

The sidecar supports per-request agent identity via `X-Agent-Id` header.
Different OpenClaw sessions can act as different ContextHub agents.

Alternatively, run multiple sidecars:

```bash
# Terminal 3a
python bridge/src/sidecar.py --port 9100 --agent-id query-agent

# Terminal 3b
python bridge/src/sidecar.py --port 9101 --agent-id analysis-agent
```

---

## Resetting state

### Reset ContextHub database

```bash
# Option A: Truncate business data only
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  TRUNCATE contexts, dependencies, change_events,
           table_metadata, lineage, table_relationships,
           query_templates, skill_versions, skill_subscriptions
  CASCADE;
"

# Option B: Full database reset
psql postgres -c "DROP DATABASE IF EXISTS contexthub;"
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
alembic upgrade head
```

### Reset OpenClaw plugin

```bash
cd /path/to/public/openclaw
pnpm openclaw plugins disable contexthub   # disable
pnpm openclaw plugins enable contexthub    # re-enable
```

To switch back to the built-in legacy context engine:

```bash
# Edit ~/.openclaw/openclaw.json
# Change plugins.slots.contextEngine to "legacy" (or remove the key)
```

---

## Stopping services

| Service           | How to stop                                |
| ----------------- | ------------------------------------------ |
| OpenClaw TUI      | `Ctrl+C` in Terminal 5                     |
| OpenClaw Gateway  | `Ctrl+C` in Terminal 4                     |
| Python Sidecar    | `Ctrl+C` in Terminal 3                     |
| ContextHub Server | `Ctrl+C` in Terminal 2                     |
| PostgreSQL        | `brew services stop postgresql@16`         |

---

## Troubleshooting

### `pnpm install` fails with SSH / host key error

```bash
cd /path/to/public/openclaw
git config url."https://github.com/".insteadOf "git@github.com:"
pnpm install
```

### `pnpm openclaw plugins install` says "missing openclaw.extensions"

The bridge `package.json` must include:

```json
"openclaw": {
  "extensions": ["./dist/index.js"]
}
```

### Plugin id mismatch warning

Ensure the bridge `package.json` has `"name": "contexthub"` (matching the
`openclaw.plugin.json` id).

### `unrecognized configuration parameter "app.account_id"`

```bash
psql postgres -c "ALTER USER contexthub WITH SUPERUSER;"
```

### Sidecar can't connect to ContextHub Server

Ensure the server is running on `:8000` before starting the sidecar.
Check `curl http://localhost:8000/health`.

### Propagation errors on server startup

Clear leftover events:

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

### OpenClaw gateway fails to start

Check that Node.js ≥ 22.12 is installed:

```bash
node --version
```

Check that the build succeeded:

```bash
cd /path/to/public/openclaw
ls dist/index.js
```

---

## Architecture reference

```
ContextHub/
├── src/contexthub/          # FastAPI server (contexts, memories, skills, search, propagation)
├── sdk/                     # Python SDK (ContextHubClient)
├── plugins/openclaw/        # Python ContextEngine plugin (assemble, afterTurn, tools)
├── bridge/
│   ├── openclaw.plugin.json # OpenClaw plugin manifest (kind: "context-engine")
│   ├── src/
│   │   ├── index.ts         # Plugin entry: register(api) → registerContextEngine + registerTool
│   │   ├── bridge.ts        # ContextHubBridge: TS ContextEngine impl → HTTP → sidecar
│   │   ├── tools.ts         # 7 MVP tool definitions (ls/read/grep/stat/store/promote/publish)
│   │   └── sidecar.py       # Python HTTP wrapper for the plugin (multi-agent via X-Agent-Id)
│   └── dist/                # Compiled JS (after npm run build)
├── scripts/demo_e2e.py      # Standalone E2E demo (no OpenClaw needed)
├── tests/                   # pytest suite
├── alembic/                 # Database migrations
└── docker-compose.yml       # PostgreSQL container (optional, Homebrew alternative above)
```
