from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bootstrap import bootstrap_repo_paths

bootstrap_repo_paths()

from contexthub_sdk import ContextHubClient

from plugin_engine import ContextHubJiuwenEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ContextHub Jiuwen Sidecar")

_engines: dict[str, ContextHubJiuwenEngine] = {}
_default_agent_id = "jiuwen-sidecar"
_server_args: dict[str, str] = {}

_STRING_FALLBACK_KEY: dict[str, str] = {
    "contexthub_store": "content",
    "ls": "path",
    "read": "uri",
    "stat": "uri",
    "grep": "query",
}


def _get_engine(request: Request | None = None) -> ContextHubJiuwenEngine:
    agent_id = _default_agent_id
    if request is not None:
        agent_id = request.headers.get("x-agent-id", _default_agent_id)
    if agent_id not in _engines:
        client = ContextHubClient(
            url=_server_args["url"],
            api_key=_server_args["api_key"],
            agent_id=agent_id,
            account_id=_server_args["account_id"],
        )
        _engines[agent_id] = ContextHubJiuwenEngine(client)
        logger.info("Created Jiuwen engine for agent_id=%s", agent_id)
    return _engines[agent_id]


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
        try:
            parsed = json.loads(raw)
            args = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            fallback_key = _STRING_FALLBACK_KEY.get(name)
            args = {fallback_key: raw} if fallback_key else {}
    logger.info("dispatch %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200])
    result = await _get_engine(request).dispatch_tool(name, args)
    return JSONResponse(content={"result": result})


@app.post("/ingest")
async def ingest(request: Request):
    body = await request.json()
    return await _get_engine(request).ingest(
        sessionId=body.get("sessionId", ""),
        message=body.get("message"),
        isHeartbeat=body.get("isHeartbeat", False),
    )


@app.post("/ingest-batch")
async def ingest_batch(request: Request):
    body = await request.json()
    return await _get_engine(request).ingestBatch(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        isHeartbeat=body.get("isHeartbeat", False),
    )


@app.post("/assemble")
async def assemble(request: Request):
    body = await request.json()
    return await _get_engine(request).assemble(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        tokenBudget=body.get("tokenBudget"),
    )


@app.post("/after-turn")
async def after_turn(request: Request):
    body = await request.json()
    await _get_engine(request).afterTurn(
        sessionId=body.get("sessionId", ""),
        messages=body.get("messages", []),
        prePromptMessageCount=body.get("prePromptMessageCount", 0),
    )
    return {"ok": True}


@app.post("/compact")
async def compact(request: Request):
    body = await request.json()
    return await _get_engine(request).compact(
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
    parser = argparse.ArgumentParser(description="ContextHub Jiuwen Sidecar")
    parser.add_argument("--port", type=int, default=9102)
    parser.add_argument("--contexthub-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="changeme")
    parser.add_argument("--agent-id", default="jiuwen-sidecar")
    parser.add_argument("--account-id", default="acme")
    args = parser.parse_args(argv)

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
