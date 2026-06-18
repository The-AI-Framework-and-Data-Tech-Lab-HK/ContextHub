"""Runtime adapters for EntCollabBench MCP services.

The helpers in this module intentionally stay independent from the external
EntCollabBench package. Tests can inject endpoint dictionaries and fake HTTP
openers, while online runs can point at ``config/mcp_endpoints_export.json``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


HttpOpen = Callable[..., Any]


class McpRuntimeAdapterError(RuntimeError):
    """Raised when the adapter cannot load endpoints or query an MCP service."""


@dataclass(frozen=True)
class McpEndpointConfig:
    """Endpoint lookup for EntCollabBench MCP servers."""

    endpoints: dict[str, str]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "McpEndpointConfig":
        raw_endpoints: Mapping[str, Any]
        if isinstance(data.get("endpoints"), Mapping):
            raw_endpoints = data["endpoints"]
        elif isinstance(data.get("mcp_endpoints"), Mapping):
            raw_endpoints = data["mcp_endpoints"]
        else:
            raw_endpoints = data

        endpoints: dict[str, str] = {}
        for server, endpoint in raw_endpoints.items():
            if not isinstance(server, str) or not server.strip():
                raise McpRuntimeAdapterError("MCP endpoint keys must be non-empty strings")
            if not isinstance(endpoint, str) or not endpoint.strip():
                raise McpRuntimeAdapterError(f"MCP endpoint for {server!r} must be a non-empty string")
            endpoints[server.strip()] = endpoint.strip()
        return cls(endpoints=endpoints)

    @classmethod
    def from_file(cls, path: str | Path) -> "McpEndpointConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise McpRuntimeAdapterError("MCP endpoint config must be a JSON object")
        return cls.from_mapping(payload)

    def endpoint(self, server: str) -> str:
        endpoint = self.endpoints.get(str(server or "").strip())
        if not endpoint:
            raise McpRuntimeAdapterError(f"Unknown MCP server: {server!r}")
        return endpoint

    def base_url(self, server: str) -> str:
        return _base_url_from_endpoint(self.endpoint(server))


def export_state(
    config: McpEndpointConfig,
    server: str,
    database_id: str,
    *,
    tables: Iterable[str] | None = None,
    where: Mapping[str, Any] | None = None,
    limit: int | None = None,
    opener: HttpOpen | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """Export database state, falling back to ``/api/database-state`` on 404/405."""

    db_id = str(database_id or "").strip()
    if not db_id:
        raise McpRuntimeAdapterError("database_id is required")

    request_tables = [str(t).strip() for t in (tables or []) if str(t).strip()]
    payload: dict[str, Any] = {"database_id": db_id}
    if request_tables:
        payload["tables"] = request_tables
    if where:
        payload["where"] = dict(where)
    if isinstance(limit, int) and limit > 0:
        payload["limit"] = limit

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-database-id": db_id,
    }
    open_http = opener or urlrequest.urlopen
    base = config.base_url(server)

    try:
        return _post_json(
            f"{base}/api/export-state",
            payload,
            headers=headers,
            opener=open_http,
            timeout=timeout,
        )
    except urlerror.HTTPError as exc:
        if exc.code not in (404, 405):
            raise McpRuntimeAdapterError(f"export-state failed for {server!r}: HTTP {exc.code}") from exc

    state = _get_json(
        f"{base}/api/database-state",
        headers=headers,
        opener=open_http,
        timeout=timeout,
    )
    if request_tables:
        state = _filter_tables(state, request_tables)
    return state


def get_tool_schema(
    config: McpEndpointConfig,
    server: str,
    tool_name: str,
    *,
    opener: HttpOpen | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Return a normalized MCP ``tools/list`` schema record for one tool."""

    target = str(tool_name or "").strip()
    if not target:
        raise McpRuntimeAdapterError("tool_name is required")

    endpoint = config.endpoint(server)
    tools = _list_tools_jsonrpc(
        endpoint,
        opener=_opener_for_endpoint(endpoint, opener=opener),
        timeout=timeout,
    )
    for item in tools:
        if str(item.get("name") or item.get("tool_name") or "").strip() == target:
            return normalize_tool_schema_record(item, tool_name=target)
    raise McpRuntimeAdapterError(f"Tool {target!r} not found on MCP server {server!r}")


def normalize_tool_schema_record(record: Mapping[str, Any], *, tool_name: str | None = None) -> dict[str, Any]:
    """Normalize MCP bridge and JSON-RPC tool-list shapes."""

    name = str(tool_name or record.get("tool_name") or record.get("name") or "").strip()
    if not name:
        raise McpRuntimeAdapterError("tool schema record is missing a tool name")
    schema = record.get("inputSchema")
    if schema is None:
        schema = record.get("input_schema")
    if not isinstance(schema, Mapping):
        schema = {"type": "object", "properties": {}, "required": []}
    return {
        "tool_name": name,
        "title": record.get("title"),
        "description": record.get("description"),
        "inputSchema": _normalize_input_schema(schema),
    }


def iter_table_rows(state: Mapping[str, Any], table: str) -> Iterator[dict[str, Any]]:
    """Yield rows from either ``table_data`` or ``tables`` state-export shapes."""

    table_name = str(table or "").strip()
    if not table_name:
        return

    table_data = state.get("table_data")
    rows = _rows_from_table_container(table_data, table_name)
    if rows is None:
        rows = _rows_from_table_container(state.get("tables"), table_name)
    for row in rows or []:
        if isinstance(row, Mapping):
            yield dict(row)


def find_rows_containing(state: Mapping[str, Any], marker: Any) -> list[dict[str, Any]]:
    """Return rows whose JSON representation contains ``marker``."""

    needle = str(marker)
    matches: list[dict[str, Any]] = []
    for table in _table_names(state):
        for row in iter_table_rows(state, table):
            if needle in json.dumps(row, ensure_ascii=False, sort_keys=True):
                matches.append({"table": table, "row": row})
    return matches


def _base_url_from_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    for suffix in ("/mcp", "/sse"):
        if endpoint.endswith(suffix):
            return endpoint[: -len(suffix)]
    return endpoint


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    opener: HttpOpen,
    timeout: int,
) -> dict[str, Any]:
    req = urlrequest.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    return _read_json_response(opener(req, timeout=timeout), url)


def _get_json(
    url: str,
    *,
    headers: Mapping[str, str],
    opener: HttpOpen,
    timeout: int,
) -> dict[str, Any]:
    req = urlrequest.Request(url=url, headers=dict(headers), method="GET")
    return _read_json_response(opener(req, timeout=timeout), url)


def _read_json_response(response: Any, url: str) -> dict[str, Any]:
    with response as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise McpRuntimeAdapterError(f"{url} returned a non-object JSON payload")
    return payload


def _read_json_response_with_headers(response: Any, url: str) -> tuple[dict[str, Any], Any]:
    with response as resp:
        raw = resp.read().decode("utf-8")
        headers = getattr(resp, "headers", None)
        if headers is None and hasattr(resp, "info"):
            headers = resp.info()
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise McpRuntimeAdapterError(f"{url} returned a non-object JSON payload")
    return payload, headers


def _post_json_with_headers(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    opener: HttpOpen,
    timeout: int,
) -> tuple[dict[str, Any], Any]:
    req = urlrequest.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    return _read_json_response_with_headers(opener(req, timeout=timeout), url)


def _opener_for_endpoint(endpoint: str, *, opener: HttpOpen | None = None) -> HttpOpen:
    if opener is not None:
        return opener
    if _is_loopback_endpoint(endpoint):
        return urlrequest.build_opener(urlrequest.ProxyHandler({})).open
    return urlrequest.urlopen


def _is_loopback_endpoint(endpoint: str) -> bool:
    try:
        host = urlparse.urlparse(endpoint).hostname
    except ValueError:
        return False
    return (host or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _header_value(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    if isinstance(headers, Mapping):
        lower_name = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lower_name and isinstance(value, str):
                return value
        return None
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value if isinstance(value, str) else None
    return None


def _list_tools_jsonrpc(endpoint: str, *, opener: HttpOpen, timeout: int) -> list[dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init, init_headers = _post_json_with_headers(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "contexthub-entcollab-adapter", "version": "1.0.0"},
            },
        },
        headers=headers,
        opener=opener,
        timeout=timeout,
    )
    session_id = _header_value(init_headers, "mcp-session-id") or init.get("mcp-session-id")
    if isinstance(session_id, str) and session_id.strip():
        headers["mcp-session-id"] = session_id.strip()

    data = _post_json(
        endpoint,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=headers,
        opener=opener,
        timeout=timeout,
    )
    tools = data.get("tools")
    if tools is None:
        tools = (data.get("result") or {}).get("tools")
    if not isinstance(tools, list):
        raise McpRuntimeAdapterError(f"Invalid tools/list result from {endpoint!r}")
    return [dict(item) for item in tools if isinstance(item, Mapping)]


def _normalize_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(schema)
    normalized.setdefault("type", "object")
    if not isinstance(normalized.get("properties"), Mapping):
        normalized["properties"] = {}
    else:
        normalized["properties"] = dict(normalized["properties"])
    required = normalized.get("required")
    if not isinstance(required, list):
        required = []
    normalized["required"] = [str(item) for item in required if isinstance(item, str) and item]
    return normalized


def _rows_from_table_container(container: Any, table: str) -> list[Any] | None:
    if isinstance(container, Mapping):
        value = container.get(table)
        if isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            rows = value.get("rows") or value.get("data")
            return rows if isinstance(rows, list) else None
    if isinstance(container, list):
        for item in container:
            if not isinstance(item, Mapping):
                continue
            if item.get("name") == table or item.get("table") == table or item.get("table_name") == table:
                rows = item.get("rows") or item.get("data")
                return rows if isinstance(rows, list) else None
    return None


def _table_names(state: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for container_name in ("table_data", "tables"):
        container = state.get(container_name)
        if isinstance(container, Mapping):
            names.extend(str(name) for name in container.keys())
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, Mapping):
                    name = item.get("name") or item.get("table") or item.get("table_name")
                    if isinstance(name, str):
                        names.append(name)
    return list(dict.fromkeys(names))


def _filter_tables(state: Mapping[str, Any], tables: Iterable[str]) -> dict[str, Any]:
    wanted = set(tables)
    filtered = dict(state)
    for key in ("table_data", "tables"):
        container = filtered.get(key)
        if isinstance(container, Mapping):
            filtered[key] = {name: value for name, value in container.items() if name in wanted}
        elif isinstance(container, list):
            filtered[key] = [
                item
                for item in container
                if isinstance(item, Mapping)
                and (item.get("name") or item.get("table") or item.get("table_name")) in wanted
            ]
    return filtered
