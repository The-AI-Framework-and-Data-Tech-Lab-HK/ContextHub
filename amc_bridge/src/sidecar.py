"""AMC v0 sidecar for OpenClaw context-engine bridge.

v0 behavior:
- ingest: write full incoming message payload to local files under openclaw_message/
- assemble: return empty context
- compact: delegated by TS bridge to OpenClaw runtime (not handled here)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="AMC v0 Sidecar")

_output_dir = Path("/home/qchenax/ContextHub/openclaw_message")
_seq = count(1)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest")
async def ingest(request: Request) -> dict[str, bool]:
    body = await request.json()
    session_id = str(body.get("sessionId") or "unknown_session")
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S.%fZ")
    seq = next(_seq)
    agent_id = request.headers.get("x-agent-id", "")
    account_id = request.headers.get("x-account-id", "")

    payload: dict[str, Any] = {
        "session_id": session_id,
        "account_id": account_id,
        "agent_id": agent_id,
        "received_at": now.isoformat(),
        "source": "ingest",
        "message": body.get("message"),
    }

    target_dir = _output_dir / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{ts}_{seq:06d}.json"
    target_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ingested": True}


@app.post("/assemble")
async def assemble(request: Request) -> dict[str, Any]:
    _ = await request.json()
    return {
        "messages": [],
        "estimatedTokens": 0,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AMC v0 sidecar")
    parser.add_argument("--port", type=int, default=9200, help="HTTP port for sidecar")
    parser.add_argument(
        "--output-dir",
        default="/home/qchenax/ContextHub/openclaw_message",
        help="Directory to persist ingested OpenClaw messages",
    )
    args = parser.parse_args(argv)

    global _output_dir
    _output_dir = Path(args.output_dir).resolve()
    _output_dir.mkdir(parents=True, exist_ok=True)

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()

