from __future__ import annotations

import json
from urllib import error as urlerror

import integrations.entcollabbench.mcp_runtime_adapter as adapter
from integrations.entcollabbench.mcp_runtime_adapter import (
    McpEndpointConfig,
    export_state,
    find_rows_containing,
    get_tool_schema,
    iter_table_rows,
)


class FakeResponse:
    def __init__(self, payload: dict, headers: dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _headers(request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def test_endpoint_config_loads_export_mapping_shape() -> None:
    config = McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"})

    assert config.endpoint("teams") == "http://127.0.0.1:8002/mcp"
    assert config.base_url("teams") == "http://127.0.0.1:8002"


def test_export_state_falls_back_to_database_state_on_404() -> None:
    config = McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"})
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/api/export-state"):
            raise urlerror.HTTPError(request.full_url, 404, "not found", hdrs=None, fp=None)
        return FakeResponse({"tables": {"messages": [{"id": "m1", "content": "hello"}]}})

    state = export_state(
        config,
        "teams",
        "db_case_001",
        tables=["messages"],
        opener=opener,
    )

    assert [call.get_method() for call in calls] == ["POST", "GET"]
    assert calls[1].full_url == "http://127.0.0.1:8002/api/database-state"
    assert _headers(calls[1])["x-database-id"] == "db_case_001"
    assert list(iter_table_rows(state, "messages")) == [{"id": "m1", "content": "hello"}]


def test_table_helpers_support_table_data_and_tables_shapes() -> None:
    table_data_state = {
        "table_data": {
            "messages": [
                {"id": "m1", "content": "needle"},
                {"id": "m2", "content": "other"},
            ]
        }
    }
    tables_state = {"tables": [{"name": "messages", "rows": [{"id": "m3", "content": "needle"}]}]}

    assert [row["id"] for row in iter_table_rows(table_data_state, "messages")] == ["m1", "m2"]
    assert [row["id"] for row in iter_table_rows(tables_state, "messages")] == ["m3"]
    assert find_rows_containing(table_data_state, "needle") == [
        {"table": "messages", "row": {"id": "m1", "content": "needle"}}
    ]


def test_get_tool_schema_uses_jsonrpc_tools_list_and_normalizes_shape() -> None:
    config = McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"})
    calls = []

    def opener(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        if calls[-1]["method"] == "initialize":
            return FakeResponse({"result": {"protocolVersion": "2024-11-05"}})
        return FakeResponse(
            {
                "result": {
                    "tools": [
                        {
                            "name": "send_channel_message",
                            "description": "Send a Teams channel message",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"teamId": {"type": "string"}},
                                "required": ["teamId"],
                            },
                        }
                    ]
                }
            }
        )

    schema = get_tool_schema(config, "teams", "send_channel_message", opener=opener)

    assert [call["method"] for call in calls] == ["initialize", "tools/list"]
    assert schema["tool_name"] == "send_channel_message"
    assert schema["inputSchema"]["required"] == ["teamId"]


def test_loopback_endpoint_default_opener_disables_system_proxy(monkeypatch) -> None:
    calls = []

    def proxy_handler(proxies):
        calls.append(("proxy", proxies))
        return ("proxy", proxies)

    class FakeOpener:
        def open(self, request, timeout):
            raise AssertionError("network should not be called")

    def build_opener(*handlers):
        calls.append(("build", handlers))
        return FakeOpener()

    monkeypatch.setattr(adapter.urlrequest, "ProxyHandler", proxy_handler)
    monkeypatch.setattr(adapter.urlrequest, "build_opener", build_opener)

    open_http = adapter._opener_for_endpoint("http://localhost:8002/mcp")

    assert open_http.__self__.__class__ is FakeOpener
    assert calls == [
        ("proxy", {}),
        ("build", (("proxy", {}),)),
    ]


def test_initialize_response_header_session_id_is_sent_to_tools_list() -> None:
    config = McpEndpointConfig.from_mapping({"teams": "http://127.0.0.1:8002/mcp"})
    request_headers = []

    def opener(request, timeout):
        request_headers.append(_headers(request))
        payload = json.loads(request.data.decode("utf-8"))
        if payload["method"] == "initialize":
            return FakeResponse(
                {"result": {"protocolVersion": "2024-11-05"}},
                headers={"Mcp-Session-Id": "session-from-header"},
            )
        return FakeResponse(
            {
                "result": {
                    "tools": [
                        {
                            "name": "send_channel_message",
                            "inputSchema": {"type": "object", "properties": {}, "required": []},
                        }
                    ]
                }
            }
        )

    get_tool_schema(config, "teams", "send_channel_message", opener=opener)

    assert "mcp-session-id" not in request_headers[0]
    assert request_headers[1]["mcp-session-id"] == "session-from-header"
