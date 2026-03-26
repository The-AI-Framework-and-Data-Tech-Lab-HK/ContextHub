"""Thin HTTP wrapper around ContextHubContextEngine.

Usage:
    python -m bridge.src.sidecar --port 9100 --contexthub-url http://localhost:8000

Exposes the Python plugin's methods as HTTP endpoints for the TypeScript bridge.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="ContextHub Sidecar")

_engine = None


def _bootstrap_repo_paths() -> list[str]:
    """Allow running the sidecar directly from a repo checkout."""
    repo_root = Path(__file__).resolve().parents[2]
    extra_paths = [
        repo_root / "sdk" / "src",
        repo_root / "plugins" / "openclaw" / "src",
    ]

    inserted: list[str] = []
    for path in extra_paths:
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
            inserted.append(path_str)
    return inserted


def _get_engine():
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    return _engine


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/info")
async def info():
    return _get_engine().info


@app.get("/tools")
async def tools():
    return _get_engine().tools


@app.post("/dispatch")
async def dispatch_tool(request: Request):
    body = await request.json()
    name = body.get("name", "")
    args = body.get("args", {})
    result = await _get_engine().dispatch_tool(name, args)
    return JSONResponse(content=json.loads(result))


@app.post("/ingest")
async def ingest(request: Request):
    body = await request.json()
    result = await _get_engine().ingest(
        sessionId=body.get("sessionId", ""),
        message=body.get("message"),
        isHeartbeat=body.get("isHeartbeat", False),
    )
    return result


@app.post("/ingest-batch")
async def ingest_batch(request: Request):
    body = await request.json()
    result = await _get_engine().ingestBatch(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        isHeartbeat=body.get("isHeartbeat", False),
    )
    return result


@app.post("/assemble")
async def assemble(request: Request):
    body = await request.json()
    result = await _get_engine().assemble(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        tokenBudget=body.get("tokenBudget"),
    )
    return result


@app.post("/after-turn")
async def after_turn(request: Request):
    body = await request.json()
    await _get_engine().afterTurn(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        prePromptMessageCount=body.get("prePromptMessageCount", 0),
    )
    return {"ok": True}


@app.post("/compact")
async def compact(request: Request):
    body = await request.json()
    result = await _get_engine().compact(
        sessionId=body.get("sessionId", ""),
        sessionFile=body.get("sessionFile"),
        tokenBudget=body.get("tokenBudget"),
        force=body.get("force", False),
    )
    return result


@app.post("/dispose")
async def dispose():
    await _get_engine().dispose()
    return {"ok": True}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ContextHub Sidecar")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--contexthub-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="changeme")
    parser.add_argument("--agent-id", default="sidecar-agent")
    parser.add_argument("--account-id", default="acme")
    args = parser.parse_args(argv)

    # Late imports to avoid requiring plugin deps at module level.
    # When running from a repo checkout, add sdk/plugin source roots first.
    _bootstrap_repo_paths()
    from contexthub_sdk import ContextHubClient
    from openclaw.plugin import ContextHubContextEngine

    global _engine
    client = ContextHubClient(
        url=args.contexthub_url,
        api_key=args.api_key,
        agent_id=args.agent_id,
        account_id=args.account_id,
    )
    _engine = ContextHubContextEngine(client)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
