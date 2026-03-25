"""7 MVP tool definitions (JSON Schema) and dispatch logic.

Each tool does: parameter conversion -> SDK call -> result formatting -> error
capture. No HTTP stack details are exposed to the agent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from contexthub_sdk import (
    ContextHubClient,
    ContextHubError,
    ContextLevel,
    ContextType,
    Scope,
)

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "ls",
        "description": "List children of a context path in ContextHub.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Context path to list children of.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read",
        "description": "Read the content of a context by URI.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Context URI to read."},
                "level": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2"],
                    "description": "Detail level. Defaults to L1.",
                },
                "version": {
                    "type": "integer",
                    "description": "Specific skill version to read.",
                },
            },
            "required": ["uri"],
        },
    },
    {
        "name": "grep",
        "description": "Search ContextHub for contexts matching a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "scope": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["datalake", "team", "agent", "user"],
                    },
                    "description": "Filter by scope.",
                },
                "context_type": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["table_schema", "skill", "memory", "resource"],
                    },
                    "description": "Filter by context type.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max results. Defaults to 5.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "stat",
        "description": "Get metadata/statistics for a context by URI.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Context URI to stat."},
            },
            "required": ["uri"],
        },
    },
    {
        "name": "contexthub_store",
        "description": "Store a private memory in ContextHub for future recall.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Memory content to store.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for the memory.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "contexthub_promote",
        "description": "Promote a private memory to team-shared context.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Memory URI to promote."},
                "target_team": {
                    "type": "string",
                    "description": "Target team to share with.",
                },
            },
            "required": ["uri", "target_team"],
        },
    },
    {
        "name": "contexthub_skill_publish",
        "description": "Publish a new version of a skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_uri": {"type": "string", "description": "Skill URI."},
                "content": {"type": "string", "description": "Skill content."},
                "changelog": {
                    "type": "string",
                    "description": "Version changelog.",
                },
                "is_breaking": {
                    "type": "boolean",
                    "description": "Whether this is a breaking change.",
                },
            },
            "required": ["skill_uri", "content"],
        },
    },
]


def _ok(data: Any) -> str:
    """Serialize a successful result to JSON string for the agent."""
    if hasattr(data, "model_dump"):
        return json.dumps(data.model_dump(mode="json"), default=str)
    return json.dumps(data, default=str)


def _err(e: Exception) -> str:
    """Serialize an error to a JSON string the agent can understand."""
    detail = getattr(e, "detail", str(e))
    return json.dumps({"error": detail})


async def dispatch(client: ContextHubClient, tool_name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool call to the appropriate SDK method."""
    try:
        if tool_name == "ls":
            result = await client.ls(args["path"])
            return _ok(result)

        if tool_name == "read":
            kwargs: dict[str, Any] = {"uri": args["uri"]}
            if "level" in args:
                kwargs["level"] = ContextLevel(args["level"])
            if "version" in args:
                kwargs["version"] = args["version"]
            result = await client.read(**kwargs)
            return _ok(result)

        if tool_name == "grep":
            kwargs = {"query": args["query"]}
            if "scope" in args:
                kwargs["scope"] = [Scope(s) for s in args["scope"]]
            if "context_type" in args:
                kwargs["context_type"] = [ContextType(ct) for ct in args["context_type"]]
            if "top_k" in args:
                kwargs["top_k"] = args["top_k"]
            result = await client.grep(**kwargs)
            return _ok(result)

        if tool_name == "stat":
            result = await client.stat(args["uri"])
            return _ok(result)

        if tool_name == "contexthub_store":
            kwargs = {"content": args["content"]}
            if "tags" in args:
                kwargs["tags"] = args["tags"]
            result = await client.memory.add(**kwargs)
            return _ok(result)

        if tool_name == "contexthub_promote":
            result = await client.memory.promote(
                uri=args["uri"], target_team=args["target_team"]
            )
            return _ok(result)

        if tool_name == "contexthub_skill_publish":
            kwargs = {
                "skill_uri": args["skill_uri"],
                "content": args["content"],
            }
            if "changelog" in args:
                kwargs["changelog"] = args["changelog"]
            if "is_breaking" in args:
                kwargs["is_breaking"] = args["is_breaking"]
            result = await client.skill.publish(**kwargs)
            return _ok(result)

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except ContextHubError as e:
        logger.warning("Tool %s failed: %s", tool_name, e.detail)
        return _err(e)
    except Exception as e:
        logger.exception("Unexpected error in tool %s", tool_name)
        return _err(e)
