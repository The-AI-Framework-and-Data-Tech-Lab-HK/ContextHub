# ContextHub OpenClaw TypeScript Bridge

HTTP sidecar + TypeScript adapter that exposes the ContextHub Python plugin
to the OpenClaw TypeScript runtime.

## Architecture

```
OpenClaw Runtime (TS)
  └─ ContextHubBridge (bridge/src/bridge.ts)
       └─ HTTP ──► Python Sidecar (bridge/src/sidecar.py)
                     └─ ContextHubContextEngine (plugins/openclaw/)
                          └─ ContextHubClient (sdk/)
                               └─ ContextHub Server API
```

## Quick Start

### 1. Start the Python sidecar

```bash
# Requires ContextHub server running on :8000
python -m bridge.src.sidecar --port 9100 --contexthub-url http://localhost:8000
```

### 2. Build the TypeScript bridge

```bash
cd bridge
npm install
npm run build
```

### 3. Use in TypeScript

```typescript
import { createContextEngine } from "@contexthub/openclaw-bridge";

const engine = createContextEngine({ sidecarUrl: "http://localhost:9100" });
await engine.fetchInfo();
const tools = await engine.fetchTools();
```

## MVP Limitations

- The TS bridge is a **scaffold**. Without a real OpenClaw runtime harness in
  this repository, we do not claim runtime-level integration.
- `compact()` returns `{ compacted: false }` — ContextHub does not own
  compaction. Real legacy delegation requires the OpenClaw runtime API.
- Tool registration depends on the OpenClaw plugin loading mechanism, which
  is outside the scope of this bridge.
