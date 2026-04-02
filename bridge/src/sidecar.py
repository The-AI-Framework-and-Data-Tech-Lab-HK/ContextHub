"""ContextHub sidecar — HTTP wrapper for the ContextHub OpenClaw plugin.

Usage:
    python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000

The sidecar wraps ContextHubContextEngine and exposes its methods as HTTP
endpoints consumed by the TypeScript bridge running inside OpenClaw.

Multi-agent support: each request can include an ``X-Agent-Id`` header.
The sidecar lazily creates one SDK client (and engine) per agent identity
so different OpenClaw sessions can act as different ContextHub agents.
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ContextHub Sidecar")

_engines: dict[str, Any] = {}
_default_agent_id: str = "sidecar-agent"
_server_args: dict[str, str] = {}

_STRING_FALLBACK_KEY: dict[str, str] = {
    "contexthub_store": "content",
    "ls": "path",
    "read": "uri",
    "stat": "uri",
    "grep": "query",
}


def _bootstrap_repo_paths() -> list[str]:
    """Add SDK and plugin source roots so the sidecar works from a repo checkout."""
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


def _get_engine(request: Request | None = None):
    """Resolve the engine for this request's agent identity."""
    agent_id = _default_agent_id
    if request is not None:
        agent_id = request.headers.get("x-agent-id", _default_agent_id)

    if agent_id not in _engines:
        from contexthub_sdk import ContextHubClient
        from openclaw.plugin import ContextHubContextEngine

        client = ContextHubClient(
            url=_server_args["url"],
            api_key=_server_args["api_key"],
            agent_id=agent_id,
            account_id=_server_args["account_id"],
        )
        _engines[agent_id] = ContextHubContextEngine(client)
        logger.info("Created engine for agent_id=%s", agent_id)

    return _engines[agent_id]


# -- HTTP endpoints -----------------------------------------------------------


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
    if isinstance(args, str):
        raw = args
        logger.info("dispatch %s: raw args string=%r", name, raw[:300])
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                args = parsed
            else:
                raise ValueError("parsed to non-dict")
        except (json.JSONDecodeError, TypeError, ValueError):
            fallback_key = _STRING_FALLBACK_KEY.get(name)
            if fallback_key:
                logger.info("dispatch %s: wrapping raw string as {%s: ...}", name, fallback_key)
                args = {fallback_key: raw}
            else:
                logger.warning("dispatch %s: cannot map raw string to params", name)
                args = {}
    logger.info("dispatch %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200])
    engine = _get_engine(request)
    result = await engine.dispatch_tool(name, args)
    return JSONResponse(content=json.loads(result))


@app.post("/ingest")
async def ingest(request: Request):
    body = await request.json()
    engine = _get_engine(request)
    return await engine.ingest(
        sessionId=body.get("sessionId", ""),
        message=body.get("message"),
        isHeartbeat=body.get("isHeartbeat", False),
    )


@app.post("/ingest-batch")
async def ingest_batch(request: Request):
    body = await request.json()
    engine = _get_engine(request)
    return await engine.ingestBatch(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        isHeartbeat=body.get("isHeartbeat", False),
    )


@app.post("/assemble")
async def assemble(request: Request):
    body = await request.json()
    engine = _get_engine(request)
    return await engine.assemble(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        tokenBudget=body.get("tokenBudget"),
    )


@app.post("/after-turn")
async def after_turn(request: Request):
    body = await request.json()
    engine = _get_engine(request)
    await engine.afterTurn(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        prePromptMessageCount=body.get("prePromptMessageCount", 0),
    )
    return {"ok": True}


@app.post("/compact")
async def compact(request: Request):
    body = await request.json()
    engine = _get_engine(request)
    return await engine.compact(
        sessionId=body.get("sessionId", ""),
        sessionFile=body.get("sessionFile"),
        tokenBudget=body.get("tokenBudget"),
        force=body.get("force", False),
    )


@app.post("/dispose")
async def dispose(request: Request):
    agent_id = request.headers.get("x-agent-id", _default_agent_id)
    engine = _engines.pop(agent_id, None)
    if engine is not None:
        await engine.dispose()
    return {"ok": True}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ContextHub Sidecar")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--contexthub-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="changeme")
    parser.add_argument("--agent-id", default="sidecar-agent")
    parser.add_argument("--account-id", default="acme")
    args = parser.parse_args(argv)

    _bootstrap_repo_paths()

    global _default_agent_id, _server_args
    _default_agent_id = args.agent_id
    _server_args = {
        "url": args.contexthub_url,
        "api_key": args.api_key,
        "account_id": args.account_id,
    }

    _get_engine()

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
