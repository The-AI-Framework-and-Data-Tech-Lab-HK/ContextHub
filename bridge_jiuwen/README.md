# ContextHub Jiuwen Bridge

JiuwenClaw integration for ContextHub.

This bridge is intentionally structured to mirror the OpenClaw `bridge`
contract where that makes sense:

- HTTP sidecar endpoints:
  - `POST /dispatch`
  - `POST /assemble`
  - `POST /after-turn`
  - `GET /health`
  - `GET /info`
  - `GET /tools`
- 7 tool definitions:
  - `ls`
  - `read`
  - `grep`
  - `stat`
  - `contexthub_store`
  - `contexthub_promote`
  - `contexthub_skill_publish`

The main difference is runtime integration:

- OpenClaw uses a TypeScript plugin and calls the sidecar over HTTP
- JiuwenClaw uses Python extension hooks directly

So this folder keeps the Jiuwen runtime path in Python, but aligns the sidecar
contract with the OpenClaw verification plan as closely as possible.

## Files

- `extension.yaml`: JiuwenClaw extension manifest
- `extension.py`: JiuwenClaw extension entry point
- `config.yaml`: local bridge defaults
- `src/bootstrap.py`: repo path bootstrap helper
- `src/tools.py`: canonical 7-tool dispatch layer
- `src/plugin_engine.py`: Python context-engine implementation modeled after the OpenClaw plugin
- `src/bridge.py`: JiuwenClaw extension hook adapter
- `src/sidecar.py`: HTTP wrapper exposing OpenClaw-style contract
- `scripts/run_mvp_verification_curl.py`: 4-curl verification runner

## Verification

```bash
cd ContextHub
source .venv/bin/activate
python bridge_jiuwen/scripts/run_mvp_verification_curl.py
```
