from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from integrations.entcollabbench.online_launcher import (
    agent_service_name,
    build_compose_override_config,
    dump_compose_override,
    wait_for_ports,
)
from integrations.entcollabbench.proxy_config import build_deployment


MCP_ENDPOINTS = {
    "calendar": "http://127.0.0.1:8003/mcp",
    "teams": "http://127.0.0.1:8002/mcp",
    "itsm": "http://127.0.0.1:8006/mcp",
}

AGENT_PEERS = {
    "it_change_engineer": "http://agent-it-change-engineer:8002",
    "customer_support_specialist": "http://agent-customer-support-specialist:8004",
}

ROLE_TO_SERVERS = {
    "collaboration_ops_specialist": ("calendar", "teams"),
    "customer_support_specialist": ("calendar", "teams"),
    "it_change_engineer": ("calendar", "itsm", "teams"),
}


def _deployment():
    return build_deployment(
        mcp_endpoints=MCP_ENDPOINTS,
        agent_peers=AGENT_PEERS,
        role_to_servers=ROLE_TO_SERVERS,
        base_port=19400,
    )


def test_agent_service_name_matches_entcollab_compose() -> None:
    assert agent_service_name("it_change_engineer") == "agent-it-change-engineer"


def test_compose_override_blanks_files_and_sets_json_env() -> None:
    config = build_compose_override_config(
        _deployment(),
        agent_ids=["collaboration_ops_specialist", "it_change_engineer"],
    )

    collab_env = config["services"]["agent-collaboration-ops-specialist"]["environment"]
    assert collab_env["MCP_ENDPOINTS_FILE"] == ""
    assert collab_env["AGENT_PEERS_FILE"] == ""

    collab_endpoints = json.loads(collab_env["MCP_ENDPOINTS_JSON"])
    assert collab_endpoints == {
        "calendar": "http://host.docker.internal:19400/mcp",
        "teams": "http://host.docker.internal:19401/mcp",
    }

    peers = json.loads(collab_env["AGENT_PEERS_JSON"])
    assert peers == {
        "customer_support_specialist": "http://host.docker.internal:19407",
        "it_change_engineer": "http://host.docker.internal:19408",
    }
    assert "host.docker.internal" in collab_env["NO_PROXY"]


def test_compose_override_can_configure_handoff_only_sender() -> None:
    config = build_compose_override_config(
        _deployment(),
        agent_ids=["finance_approval_specialist"],
        include_tools=True,
        include_handoff=True,
    )

    env = config["services"]["agent-finance-approval-specialist"]["environment"]
    assert "MCP_ENDPOINTS_JSON" not in env
    assert env["AGENT_PEERS_FILE"] == ""
    assert json.loads(env["AGENT_PEERS_JSON"])["it_change_engineer"] == "http://host.docker.internal:19408"


def test_dump_compose_override_quotes_json_env() -> None:
    text = dump_compose_override(
        build_compose_override_config(_deployment(), agent_ids=["it_change_engineer"])
    )

    assert "services:" in text
    assert "agent-it-change-engineer:" in text
    assert 'MCP_ENDPOINTS_FILE: ""' in text
    assert "MCP_ENDPOINTS_JSON: '{" in text
    assert "AGENT_PEERS_JSON: '{" in text


def test_wait_for_ports_observes_ready_listener() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        wait_for_ports([httpd.server_address[1]], timeout_seconds=1.0)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1)


def test_wait_for_ports_times_out_for_missing_listener() -> None:
    with pytest.raises(TimeoutError):
        wait_for_ports([9], timeout_seconds=0.05, interval_seconds=0.01)
