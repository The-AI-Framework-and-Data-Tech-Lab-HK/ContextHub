# ContextHub OpenClaw Plugin

OpenClaw context-engine plugin that connects ContextHub (enterprise context
management) to the OpenClaw agent runtime.

For complete setup, follow the [OpenClaw integration guide](../docs/setup/openclaw-integration-guide.md). For a Python-only Deep Agents connection, use the [Deep Agents tutorial](../docs/setup/deepagents-integration-guide.md); it does not need this bridge or sidecar.

## Architecture

```
OpenClaw Runtime (TS)
  ├─ ContextHubBridge (bridge/src/bridge.ts)    ← ContextEngine lifecycle
  │    └─ HTTP ──► Python Sidecar (bridge/src/sidecar.py)
  │                  └─ ContextHubContextEngine (plugins/openclaw/)
  │                       └─ ContextHubClient (sdk/)
  │                            └─ ContextHub Server API (:8000)
  │
  └─ 8 contexthub_ Agent Tools (bridge/src/tools.ts)
       └─ HTTP ──► Python Sidecar /dispatch
```

## Prerequisites

- **ContextHub Server** running on `:8000` (PostgreSQL + pgvector + FastAPI)
- **Python 3.11+** with `httpx`, `fastapi`, `uvicorn` for the sidecar
- **Node.js ≥ 22.12** (required by OpenClaw)
- **pnpm** for OpenClaw monorepo

The quick start below runs the server and sidecar in one environment, which needs Python 3.12+ because of the server's requirements.

## Quick Start

### 1. Start ContextHub

```bash
cd ContextHub
python -m pip install -e . -e ./sdk -e ./plugins/openclaw greenlet
docker compose up -d         # PostgreSQL + pgvector
alembic upgrade head          # Run migrations
uvicorn contexthub.main:app --port 8000
```

### 2. Start the Python sidecar

```bash
cd ContextHub
python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000
```

The sidecar supports multi-agent mode: pass `X-Agent-Id` header per request
to use different ContextHub agent identities.

### 3. Build and install the plugin in OpenClaw

```bash
# Build the TS bridge
cd ContextHub/bridge
npm ci
npm run build

# Install as a local OpenClaw plugin (link mode for development)
cd /path/to/openclaw
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

### 4. Configure OpenClaw

Add to your `~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    slots: {
      contextEngine: "contexthub"
    },
    entries: {
      contexthub: {
        enabled: true,
        config: {
          sidecarUrl: "http://localhost:9100"
        }
      }
    }
  }
}
```

### 5. Run OpenClaw

```bash
cd /path/to/openclaw
pnpm openclaw
```

## What the plugin provides

### Context Engine lifecycle

| Method      | Behavior |
|-------------|----------|
| `assemble`  | Auto-recall: searches ContextHub for context relevant to the current conversation and injects it via `systemPromptAddition` |
| `afterTurn` | Auto-capture: extracts reusable facts from assistant responses and writes them as private memories in ContextHub |
| `compact`   | Delegates to OpenClaw's built-in runtime compaction (`delegateCompactionToRuntime`) |
| `ingest`    | No-op (ContextHub does not manage conversation history) |

### Agent tools

| Tool | Description |
|------|-------------|
| `contexthub_ls` | List contexts under a URI prefix |
| `contexthub_read` | Read a context by URI |
| `contexthub_search` | Search ContextHub; returns sources and a retrieval ID |
| `contexthub_stat` | Get context metadata |
| `contexthub_store` | Save a private memory |
| `contexthub_promote` | Promote private memory to team scope |
| `contexthub_skill_publish` | Publish a new skill version |
| `contexthub_feedback` | Report whether retrieved context was useful |

Tool names match the corresponding Deep Agents tools. The unprefixed tool names
are not registered or accepted. After updating, rebuild the bridge and restart
both the sidecar and OpenClaw; update any tool-name references in your own prompts
or configuration.

## Multi-agent testing

To simulate multiple agents, run one sidecar per agent identity:

```bash
python bridge/src/sidecar.py --port 9100 --agent-id query-agent
python bridge/src/sidecar.py --port 9101 --agent-id analysis-agent
```

Or use the dynamic `X-Agent-Id` header — the sidecar lazily creates separate
SDK clients per agent identity.

## Verifying the integration

Run the bridge registration/forwarding tests from `bridge/` with `npm test`.
From the repository root, run the Python plugin and sidecar tests with
`python -m pytest tests/test_plugin.py`. Their database integration test remains
opt-in.

```bash
# Check sidecar health
curl http://localhost:9100/health

# List available tools
curl http://localhost:9100/tools

# Verify context engine is active
cd /path/to/openclaw && pnpm openclaw doctor
```
