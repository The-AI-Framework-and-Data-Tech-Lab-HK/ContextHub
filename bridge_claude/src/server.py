from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

try:
    import fakeredis.aioredis as _fakeredis_aioredis

    if not hasattr(_fakeredis_aioredis, "FakeConnection") and hasattr(
        _fakeredis_aioredis, "FakeRedisConnection"
    ):
        _fakeredis_aioredis.FakeConnection = _fakeredis_aioredis.FakeRedisConnection
except Exception:
    pass

from fastmcp import FastMCP
from fastmcp.server.server import FastMCP as FastMCPServer

from bootstrap import bootstrap_repo_paths

bootstrap_repo_paths()

from contexthub_sdk import ContextHubClient
from tools import dispatch


def build_client() -> ContextHubClient:
    return ContextHubClient(
        url=os.getenv("CONTEXTHUB_URL", "http://127.0.0.1:8000"),
        api_key=os.getenv("CONTEXTHUB_API_KEY", "changeme"),
        account_id=os.getenv("CONTEXTHUB_ACCOUNT_ID", "acme"),
        agent_id=os.getenv("CONTEXTHUB_AGENT_ID", "claude-agent"),
    )


def parse_tool_output(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw


async def call_tool(name: str, args: dict[str, Any]) -> Any:
    client = build_client()
    try:
        return parse_tool_output(await dispatch(client, name, args))
    finally:
        await client.aclose()


instructions = (
    "Use ContextHub tools when the user asks to remember, save, share, promote, "
    "list, read, or search reusable context. "
    "Use contexthub_store for private memory, contexthub_promote for shared team memory."
)


@asynccontextmanager
async def _no_docket_lifespan(self: FastMCPServer):
    yield


# This server only exposes synchronous MCP tools. Disabling FastMCP's Docket layer
# avoids an incompatible fakeredis import path in the bundled task subsystem.
FastMCPServer._docket_lifespan = _no_docket_lifespan

mcp = FastMCP("ContextHub Claude Bridge", instructions=instructions, tasks=False)


@mcp.tool(
    name="ls",
    description="List children of a ContextHub path. Use for shared or private memory folders.",
)
async def ls(path: str) -> Any:
    return await call_tool("ls", {"path": path})


@mcp.tool(
    name="read",
    description="Read a ContextHub URI. Use to inspect a memory or skill by URI.",
)
async def read(uri: str, level: str | None = None, version: int | None = None) -> Any:
    args: dict[str, Any] = {"uri": uri}
    if level is not None:
        args["level"] = level
    if version is not None:
        args["version"] = version
    return await call_tool("read", args)


@mcp.tool(
    name="grep",
    description="Search ContextHub for relevant memories, skills, or schemas.",
)
async def grep(
    query: str,
    scope: list[str] | None = None,
    context_type: list[str] | None = None,
    top_k: int | None = None,
) -> Any:
    args: dict[str, Any] = {"query": query}
    if scope:
        args["scope"] = scope
    if context_type:
        args["context_type"] = context_type
    if top_k is not None:
        args["top_k"] = top_k
    return await call_tool("grep", args)


@mcp.tool(
    name="stat",
    description="Get metadata for a ContextHub URI.",
)
async def stat(uri: str) -> Any:
    return await call_tool("stat", {"uri": uri})


@mcp.tool(
    name="contexthub_store",
    description=(
        "Store a private memory in ContextHub. Use when the user says remember, save, "
        "note this down, or keep this for later."
    ),
)
async def contexthub_store(content: str, tags: list[str] | None = None) -> Any:
    args: dict[str, Any] = {"content": content}
    if tags:
        args["tags"] = tags
    return await call_tool("contexthub_store", args)


@mcp.tool(
    name="contexthub_promote",
    description=(
        "Promote a private memory into team-shared space. Use when the user asks to "
        "share or publish a saved memory to a team."
    ),
)
async def contexthub_promote(uri: str, target_team: str) -> Any:
    return await call_tool("contexthub_promote", {"uri": uri, "target_team": target_team})


@mcp.tool(
    name="contexthub_skill_publish",
    description="Publish a new version of a ContextHub skill.",
)
async def contexthub_skill_publish(
    skill_uri: str,
    content: str,
    changelog: str | None = None,
    is_breaking: bool | None = None,
) -> Any:
    args: dict[str, Any] = {"skill_uri": skill_uri, "content": content}
    if changelog is not None:
        args["changelog"] = changelog
    if is_breaking is not None:
        args["is_breaking"] = is_breaking
    return await call_tool("contexthub_skill_publish", args)


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
