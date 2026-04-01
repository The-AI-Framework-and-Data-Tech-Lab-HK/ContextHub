# AMC v0 + OpenClaw Integration Guide

This guide explains how to connect the **AMC v0 context-engine plugin** into OpenClaw.

v0 is intentionally minimal:
- `ingest`: record full message payload to local files
- `assemble`: return empty context (`messages: []`)
- `compact`: delegated to OpenClaw built-in compaction

## 1) Build the AMC bridge plugin

```bash
cd /home/qchenax/ContextHub/amc_bridge
npm install
npm run build
```

Expected output artifact:
- `amc_bridge/dist/index.js`

## 2) Install plugin into OpenClaw

```bash
cd /path/to/openclaw
pnpm openclaw plugins install -l /home/qchenax/ContextHub/amc_bridge
```

Then set plugin slot/config in `~/.openclaw/openclaw.json`:

```json5
{
  "plugins": {
    "slots": {
      "contextEngine": "amc"
    },
    "entries": {
      "amc": {
        "enabled": true,
        "config": {
          "sidecarUrl": "http://localhost:9200"
        }
      }
    }
  }
}
```

## 3) Start AMC v0 sidecar

```bash
cd /home/qchenax/ContextHub
source .venv/bin/activate
python amc_bridge/src/sidecar.py \
  --port 9200 \
  --output-dir /home/qchenax/ContextHub/openclaw_message
```

Quick health check:

```bash
curl http://localhost:9200/health
# {"status":"ok"}
```

## 4) Start OpenClaw gateway and TUI

In OpenClaw repo:

```bash
pnpm openclaw gateway
pnpm openclaw tui
```

Chat in TUI to trigger context-engine hooks.

## 5) Verify v0 behavior

### One-command smoke test (curl based)

```bash
cd /home/qchenax/ContextHub
bash scripts/test_amc_openclaw_v0_sidecar.sh
```

Optional env overrides:

```bash
SIDECAR_URL=http://127.0.0.1:9200 \
SESSION_ID=smoke-session-002 \
OUTPUT_DIR=/home/qchenax/ContextHub/openclaw_message \
bash scripts/test_amc_openclaw_v0_sidecar.sh
```

### Verify ingest file output

After sending messages in TUI, check:

```bash
ls /home/qchenax/ContextHub/openclaw_message
```

Files are written under:
- `openclaw_message/{session_id}/{timestamp}_{seq}.json`

Each file includes:
- `session_id`
- `account_id` (if provided by runtime headers)
- `agent_id` (if provided by runtime headers)
- `received_at`
- `source` (`ingest`)
- `message` (full message object)
- `raw_request`

### Verify assemble returns empty

```bash
curl -X POST http://localhost:9200/assemble \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"test","messages":[{"role":"user","content":"hello"}]}'
```

Expected:

```json
{
  "messages": [],
  "estimatedTokens": 0
}
```

### Verify compact delegation

`compact()` is handled in TS bridge via `delegateCompactionToRuntime(...)`.
No sidecar `/compact` endpoint is required for v0.

## 6) Troubleshooting

### Plugin install says missing `openclaw.extensions`

Ensure `amc_bridge/package.json` contains:

```json
"openclaw": {
  "extensions": ["./dist/index.js"]
}
```

### OpenClaw cannot load plugin

- Confirm plugin id matches: `amc_bridge/openclaw.plugin.json` has `"id": "amc"`.
- Confirm `dist/index.js` exists after build.
- Restart gateway after install/config changes.

### Sidecar is up but no files written

- Confirm OpenClaw slot is set to `"amc"` and entry enabled.
- Confirm sidecar URL in OpenClaw config matches running port (`9200` by default).
- Verify sidecar logs for `/ingest` hits.

## 7) What comes next (v1+)

After v0 connectivity is stable, evolve to:
- `assemble` -> AMC semantic retrieve path
- `ingestBatch/afterTurn` -> commit/index pipeline
- optional AMC-owned compaction (`ownsCompaction=true`)

