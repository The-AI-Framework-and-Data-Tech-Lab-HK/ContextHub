from __future__ import annotations

import json

from integrations.entcollabbench.proxy_config import (
    build_deployment,
    load_entcollab_maps,
    write_deployment,
)


MCP_ENDPOINTS = {
    "calendar": "http://mcp-calendar:8003/mcp",
    "teams": "http://mcp-teams:8002/mcp",
    "itsm": "http://mcp-itsm:8006/mcp",
    "hr": "http://mcp-hr:8010/mcp",
}

AGENT_PEERS = {
    "it_service_desk_l1": "http://agent-it-service-desk-l1:8001",
    "hr_service_specialist": "http://agent-hr-service-specialist:8003",
}

ROLE_TO_SERVERS = {
    "it_service_desk_l1": ("calendar", "itsm", "teams"),
    "hr_service_specialist": ("calendar", "hr", "teams"),
    "finance_approval_specialist": ("workspace",),  # no MCP server → no tool routes
}


def _deployment():
    return build_deployment(
        mcp_endpoints=MCP_ENDPOINTS,
        agent_peers=AGENT_PEERS,
        role_to_servers=ROLE_TO_SERVERS,
        bind_host="127.0.0.1",
        reach_host="host.docker.internal",
        base_port=19000,
    )


def test_tool_routes_only_for_known_servers_excluding_workspace() -> None:
    dep = _deployment()
    pairs = {(t.route.agent_id, t.route.server) for t in dep.tool_listeners}

    assert ("it_service_desk_l1", "itsm") in pairs
    assert ("hr_service_specialist", "hr") in pairs
    # approval agent maps only to workspace, which is not an MCP server
    assert not any(agent == "finance_approval_specialist" for agent, _ in pairs)
    assert all(server != "workspace" for _, server in pairs)
    # 2 agents × 3 in-map servers each = 6 tool listeners
    assert len(dep.tool_listeners) == 6


def test_ports_are_unique_and_deterministic() -> None:
    dep = _deployment()
    ports = dep.all_ports()
    assert len(ports) == len(set(ports))
    assert min(ports) == 19000
    # 6 tool + 2 handoff = 8 listeners, contiguous from base
    assert sorted(ports) == list(range(19000, 19008))
    # determinism: same inputs → same assignment
    assert _deployment().all_ports() == ports


def test_handoff_listener_per_recipient_with_preserved_upstream() -> None:
    dep = _deployment()
    by_agent = {h.route.target_agent: h for h in dep.handoff_listeners}
    assert set(by_agent) == set(AGENT_PEERS)
    assert by_agent["it_service_desk_l1"].route.upstream_endpoint == AGENT_PEERS["it_service_desk_l1"]


def test_mcp_endpoints_by_agent_points_each_server_to_its_listener() -> None:
    dep = _deployment()
    by_agent = dep.mcp_endpoints_by_agent()

    assert set(by_agent["it_service_desk_l1"]) == {"calendar", "itsm", "teams"}
    for listener in dep.tool_listeners:
        assert (
            by_agent[listener.route.agent_id][listener.route.server]
            == listener.reach_url
            == f"http://host.docker.internal:{listener.port}/mcp"
        )


def test_rewritten_agent_peers_point_to_handoff_listeners() -> None:
    dep = _deployment()
    peers = dep.rewritten_agent_peers()
    for listener in dep.handoff_listeners:
        assert peers[listener.route.target_agent] == listener.reach_url
        assert listener.reach_url == f"http://host.docker.internal:{listener.port}"


def test_write_deployment_emits_per_agent_files_and_manifest(tmp_path) -> None:
    dep = _deployment()
    artifacts = write_deployment(dep, tmp_path)

    assert (tmp_path / "mcp_endpoints.it_service_desk_l1.json").exists()
    assert (tmp_path / "agent_peers.json").exists()
    manifest = json.loads((tmp_path / "proxy_routes.json").read_text())
    assert len(manifest["tool_listeners"]) == 6
    assert len(manifest["handoff_listeners"]) == 2

    peers = json.loads((tmp_path / "agent_peers.json").read_text())
    assert set(peers) == set(AGENT_PEERS)


def test_load_entcollab_maps_roundtrip(tmp_path) -> None:
    (tmp_path / "mcp_endpoints.json").write_text(json.dumps(MCP_ENDPOINTS), encoding="utf-8")
    (tmp_path / "agent_peers.json").write_text(json.dumps(AGENT_PEERS), encoding="utf-8")

    mcp, peers = load_entcollab_maps(tmp_path)

    assert mcp == MCP_ENDPOINTS
    assert peers == AGENT_PEERS
