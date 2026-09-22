"""Deployment generator for the ContextHub online proxies (option A).

This turns the EntCollabBench URL maps into a deterministic fleet of per-listener
routes plus the rewritten config the agents should read, so the proxies can be
inserted **by config only** (no EntCollabBench source edits):

- **tool gate** — agents resolve MCP via ``MCP_ENDPOINTS_FILE`` (per-agent
  overridable env, verified in ``agent/docker-compose.yml``). We emit one MCP
  proxy listener per ``(agent, server)`` (option A: identity by port) and a
  per-agent endpoints file pointing each server at that agent's listener.
- **handoff gate** — agents resolve peers via ``agent_peers.json`` and POST to
  ``{peer}/v1/agent/tasks`` (verified in ``agent.py::_create_http_delegation_tool``).
  The recipient identity is fixed by the peer entry, so one listener per
  recipient agent suffices; we emit a rewritten ``agent_peers`` map.

Listeners bind on ``bind_host`` and are addressed by containers via
``reach_host`` (default ``host.docker.internal``, which the compose file exposes).
Everything here is pure/deterministic and unit-testable without Docker; bringing
up services and wiring the override is the operator step (B).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from integrations.entcollabbench.mapping import ROLE_TO_SERVERS
from integrations.entcollabbench.online_handoff_proxy import HandoffProxyRoute
from integrations.entcollabbench.online_proxy import ToolProxyRoute

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_REACH_HOST = "host.docker.internal"
DEFAULT_BASE_PORT = 19000


@dataclass(frozen=True)
class ToolListener:
    """One MCP tool-proxy listener bound to a single (agent, server)."""

    route: ToolProxyRoute
    bind_host: str
    port: int
    reach_url: str  # what an agent container POSTs to, e.g. http://host.docker.internal:PORT/mcp


@dataclass(frozen=True)
class HandoffListener:
    """One handoff-proxy listener fronting a single recipient agent."""

    route: HandoffProxyRoute
    bind_host: str
    port: int
    reach_url: str  # peer base an agent container delegates to, e.g. http://host.docker.internal:PORT


@dataclass(frozen=True)
class ProxyDeployment:
    """The full deterministic listener fleet + rewritten agent-facing config."""

    tool_listeners: tuple[ToolListener, ...]
    handoff_listeners: tuple[HandoffListener, ...]

    def mcp_endpoints_by_agent(self) -> dict[str, dict[str, str]]:
        """Per-agent ``MCP_ENDPOINTS_FILE`` content: server → that agent's listener."""

        out: dict[str, dict[str, str]] = {}
        for listener in self.tool_listeners:
            out.setdefault(listener.route.agent_id, {})[listener.route.server] = listener.reach_url
        return out

    def rewritten_agent_peers(self) -> dict[str, str]:
        """Rewritten ``agent_peers.json``: recipient → its handoff listener."""

        return {listener.route.target_agent: listener.reach_url for listener in self.handoff_listeners}

    def all_ports(self) -> list[int]:
        return [listener.port for listener in self.tool_listeners] + [
            listener.port for listener in self.handoff_listeners
        ]


def build_deployment(
    *,
    mcp_endpoints: Mapping[str, str],
    agent_peers: Mapping[str, str],
    role_to_servers: Mapping[str, Iterable[str]] = ROLE_TO_SERVERS,
    bind_host: str = DEFAULT_BIND_HOST,
    reach_host: str = DEFAULT_REACH_HOST,
    base_port: int = DEFAULT_BASE_PORT,
) -> ProxyDeployment:
    """Assign deterministic ports + routes for the tool and handoff proxies.

    Tool listeners are created for each ``(agent, server)`` where the agent's
    toolset (``role_to_servers``) includes a server present in ``mcp_endpoints``
    (so ``workspace``-only approval agents get none). Handoff listeners are
    created per recipient agent in ``agent_peers``.
    """

    port = base_port
    tool_listeners: list[ToolListener] = []
    for agent in sorted(role_to_servers):
        for server in sorted(role_to_servers[agent]):
            upstream = mcp_endpoints.get(server)
            if not upstream:
                continue
            tool_listeners.append(
                ToolListener(
                    route=ToolProxyRoute(agent_id=agent, server=server, upstream_endpoint=upstream),
                    bind_host=bind_host,
                    port=port,
                    reach_url=f"http://{reach_host}:{port}/mcp",
                )
            )
            port += 1

    handoff_listeners: list[HandoffListener] = []
    for agent in sorted(agent_peers):
        upstream = agent_peers[agent]
        if not upstream:
            continue
        handoff_listeners.append(
            HandoffListener(
                route=HandoffProxyRoute(target_agent=agent, upstream_endpoint=upstream),
                bind_host=bind_host,
                port=port,
                reach_url=f"http://{reach_host}:{port}",
            )
        )
        port += 1

    return ProxyDeployment(
        tool_listeners=tuple(tool_listeners),
        handoff_listeners=tuple(handoff_listeners),
    )


def load_entcollab_maps(
    config_dir: str | Path,
    *,
    mcp_endpoints_file: str = "mcp_endpoints.json",
    agent_peers_file: str = "agent_peers.json",
) -> tuple[dict[str, str], dict[str, str]]:
    """Load the (server→url) and (agent→url) maps from an EntCollabBench config dir.

    Defaults use the docker-internal files the agent containers actually read
    (``mcp_endpoints.json`` via ``MCP_ENDPOINTS_FILE``; ``agent_peers.json`` via
    ``AGENT_PEERS_FILE``). Pass ``*_export.json`` for the host-network view.
    """

    base = Path(config_dir)
    mcp_endpoints = _load_str_map(base / mcp_endpoints_file)
    agent_peers = _load_str_map(base / agent_peers_file)
    return mcp_endpoints, agent_peers


def write_deployment(deployment: ProxyDeployment, out_dir: str | Path) -> dict[str, str]:
    """Write per-agent MCP endpoints files, rewritten agent_peers, and a manifest.

    Returns a map of artifact name → path. The operator points each agent's
    ``MCP_ENDPOINTS_FILE`` at ``mcp_endpoints.<agent>.json`` and mounts the
    rewritten ``agent_peers.json`` (or overrides ``AGENT_PEERS_FILE``).
    """

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    for agent, endpoints in deployment.mcp_endpoints_by_agent().items():
        path = out / f"mcp_endpoints.{agent}.json"
        path.write_text(json.dumps(endpoints, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifacts[f"mcp_endpoints:{agent}"] = str(path)

    peers_path = out / "agent_peers.json"
    peers_path.write_text(
        json.dumps(deployment.rewritten_agent_peers(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifacts["agent_peers"] = str(peers_path)

    manifest_path = out / "proxy_routes.json"
    manifest_path.write_text(
        json.dumps(_manifest(deployment), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    artifacts["manifest"] = str(manifest_path)
    return artifacts


def _manifest(deployment: ProxyDeployment) -> dict[str, Any]:
    return {
        "tool_listeners": [
            {
                "agent_id": listener.route.agent_id,
                "server": listener.route.server,
                "bind_host": listener.bind_host,
                "port": listener.port,
                "reach_url": listener.reach_url,
                "upstream_endpoint": listener.route.upstream_endpoint,
            }
            for listener in deployment.tool_listeners
        ],
        "handoff_listeners": [
            {
                "target_agent": listener.route.target_agent,
                "bind_host": listener.bind_host,
                "port": listener.port,
                "reach_url": listener.reach_url,
                "upstream_endpoint": listener.route.upstream_endpoint,
            }
            for listener in deployment.handoff_listeners
        ],
    }


def _load_str_map(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return {str(key): str(value) for key, value in payload.items()}
