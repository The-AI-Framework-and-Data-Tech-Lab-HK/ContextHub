"""Operational launcher for EntCollabBench online ContextHub proxies.

This is the productized counterpart to ``_observe_smoke``. It still avoids any
EntCollabBench source edits: the operator starts host-side proxy listeners, then
uses a generated compose override to point agent containers at per-agent MCP
endpoints and the rewritten handoff peer map.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
import json
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import asyncpg

from contexthub.config import Settings
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import ScopedRepo
from integrations.entcollabbench.mapping import ROLE_TO_SERVERS
from integrations.entcollabbench.mcp_runtime_adapter import McpEndpointConfig
from integrations.entcollabbench.online_audit import build_pg_enforcement_audit_sink
from integrations.entcollabbench.online_handoff_proxy import (
    build_full_s2_handoff_proxy,
    build_s1_handoff_proxy,
    build_weak_s2_handoff_proxy,
)
from integrations.entcollabbench.online_proxy import (
    build_full_s2_tool_proxy,
    build_s1_tool_proxy,
    build_weak_s2_tool_proxy,
)
from integrations.entcollabbench.online_proxy_base import JsonlDecisionSink, ProxyHTTPServer
from integrations.entcollabbench.proxy_config import (
    ProxyDeployment,
    build_deployment,
    load_entcollab_maps,
    write_deployment,
)
from integrations.entcollabbench.world_loader import WorldLoader


DEFAULT_AGENTS = tuple(ROLE_TO_SERVERS)


def agent_service_name(agent_id: str) -> str:
    """Return the EntCollabBench docker compose service for an agent id."""

    return f"agent-{str(agent_id).replace('_', '-')}"


def build_compose_override_config(
    deployment: ProxyDeployment,
    *,
    agent_ids: Iterable[str] | None = None,
    include_tools: bool = True,
    include_handoff: bool = True,
    no_proxy_extra: Iterable[str] = ("host.docker.internal",),
) -> dict[str, Any]:
    """Build a docker-compose override dict for route-B online identity.

    ``MCP_ENDPOINTS_FILE`` / ``AGENT_PEERS_FILE`` must be blanked, because the
    pinned agent code only reads the JSON env fallback when the file path is
    empty or missing. This lets each agent get its own MCP endpoint map without
    changing the read-only shared config mount.
    """

    endpoints_by_agent = deployment.mcp_endpoints_by_agent()
    rewritten_peers = deployment.rewritten_agent_peers()
    selected = set(agent_ids or ())
    if not selected:
        selected.update(endpoints_by_agent)
        selected.update(rewritten_peers)

    services: dict[str, Any] = {}
    for agent in sorted(selected):
        env: dict[str, str] = {}
        endpoints = endpoints_by_agent.get(agent)
        if include_tools and endpoints:
            env["MCP_ENDPOINTS_FILE"] = ""
            env["MCP_ENDPOINTS_JSON"] = _json_env(endpoints)
        if include_handoff and rewritten_peers:
            env["AGENT_PEERS_FILE"] = ""
            env["AGENT_PEERS_JSON"] = _json_env(rewritten_peers)
        if no_proxy_extra:
            suffix = ",".join(dict.fromkeys(str(item) for item in no_proxy_extra if str(item)))
            if suffix:
                env["NO_PROXY"] = f"${{NO_PROXY:-localhost,127.0.0.1,redis}},{suffix}"
                env["no_proxy"] = f"${{no_proxy:-localhost,127.0.0.1,redis}},{suffix}"
        if env:
            services[agent_service_name(agent)] = {"environment": env}

    return {"services": services}


def dump_compose_override(config: Mapping[str, Any]) -> str:
    """Dump the small compose override shape without adding a YAML dependency."""

    lines = ["services:"]
    services = config.get("services") if isinstance(config, Mapping) else None
    if not isinstance(services, Mapping):
        return "services: {}\n"
    for service, spec in services.items():
        lines.append(f"  {service}:")
        env = spec.get("environment") if isinstance(spec, Mapping) else None
        if not isinstance(env, Mapping) or not env:
            lines.append("    environment: {}")
            continue
        lines.append("    environment:")
        for key in sorted(env):
            lines.append(f"      {key}: {_yaml_scalar(str(env[key]))}")
    return "\n".join(lines) + "\n"


def write_compose_override(
    deployment: ProxyDeployment,
    path: str | Path,
    *,
    agent_ids: Iterable[str] | None = None,
    include_tools: bool = True,
    include_handoff: bool = True,
) -> Path:
    """Write a compose override that points agents at the generated proxy maps."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    config = build_compose_override_config(
        deployment,
        agent_ids=agent_ids,
        include_tools=include_tools,
        include_handoff=include_handoff,
    )
    target.write_text(dump_compose_override(config), encoding="utf-8")
    return target


class ProxyFleet:
    """Owns a set of started proxy listeners and side-channel audit sinks."""

    def __init__(self, servers: Iterable[ProxyHTTPServer], audit_sinks: Iterable[Any] = ()):
        self._servers = list(servers)
        self._audit_sinks = list(audit_sinks)

    @property
    def ports(self) -> list[int]:
        return [server.address[1] for server in self._servers]

    def wait_ready(self, *, timeout_seconds: float = 10.0) -> None:
        wait_for_ports(self.ports, timeout_seconds=timeout_seconds)

    def stop(self) -> None:
        for server in reversed(self._servers):
            server.stop()
        for sink in self._audit_sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                close(drain=True)


def wait_for_ports(
    ports: Iterable[int],
    *,
    host: str = "127.0.0.1",
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.1,
) -> None:
    """Block until all ports accept TCP connections, or raise TimeoutError."""

    pending = {int(port) for port in ports}
    deadline = time.monotonic() + timeout_seconds
    while pending and time.monotonic() < deadline:
        ready = {port for port in pending if _port_accepts(host, port)}
        pending.difference_update(ready)
        if pending:
            time.sleep(interval_seconds)
    if pending:
        raise TimeoutError(f"proxy ports did not become ready: {sorted(pending)}")


def start_proxy_fleet(
    deployment: ProxyDeployment,
    *,
    tool_proxy: Any | None,
    handoff_proxy: Any | None,
    audit_sinks: Iterable[Any] = (),
) -> ProxyFleet:
    """Start tool and/or handoff listeners for a deployment."""

    servers: list[ProxyHTTPServer] = []
    if tool_proxy is not None:
        for listener in deployment.tool_listeners:
            servers.append(
                ProxyHTTPServer(
                    listener.route,
                    tool_proxy,
                    host=listener.bind_host,
                    port=listener.port,
                ).start()
            )
    if handoff_proxy is not None:
        for listener in deployment.handoff_listeners:
            servers.append(
                ProxyHTTPServer(
                    listener.route,
                    handoff_proxy,
                    host=listener.bind_host,
                    port=listener.port,
                ).start()
            )
    return ProxyFleet(servers, audit_sinks=audit_sinks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True, help="EntCollabBench config directory")
    parser.add_argument("--mcp-endpoints-file", default="mcp_endpoints_export.json")
    parser.add_argument("--agent-peers-file", default="agent_peers.json")
    parser.add_argument("--agents", nargs="*", default=list(DEFAULT_AGENTS))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True, help="combined decision-log JSONL path")
    parser.add_argument("--compose-override", type=Path, default=None)
    parser.add_argument("--base-port", type=int, default=19000)
    parser.add_argument("--mode", choices=["observe", "enforce"], default="observe")
    parser.add_argument("--system", choices=["S1", "S2"], default="S2")
    parser.add_argument("--tool", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--handoff", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--full-s2", action="store_true")
    parser.add_argument("--world-json", type=Path)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--account-id", default="entcollab-runtime")
    parser.add_argument("--audit-account-id", default=None)
    parser.add_argument("--run-id", default="entcollab-online")
    parser.add_argument("--no-audit", action="store_true")
    parser.add_argument("--health-timeout-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    dsn = args.database_url or Settings().asyncpg_database_url
    mcp_endpoints, agent_peers = load_entcollab_maps(
        args.config_dir,
        mcp_endpoints_file=args.mcp_endpoints_file,
        agent_peers_file=args.agent_peers_file,
    )
    role_to_servers = {agent: ROLE_TO_SERVERS[agent] for agent in args.agents if agent in ROLE_TO_SERVERS}
    deployment = build_deployment(
        mcp_endpoints=mcp_endpoints,
        agent_peers=agent_peers,
        role_to_servers=role_to_servers,
        base_port=args.base_port,
    )
    artifacts = write_deployment(deployment, args.out_dir)
    if args.compose_override is not None:
        artifacts["compose_override"] = str(
            write_compose_override(
                deployment,
                args.compose_override,
                agent_ids=args.agents,
                include_tools=args.tool,
                include_handoff=args.handoff,
            )
        )

    sink = JsonlDecisionSink(args.log)
    audit_sink = None
    if args.system == "S2" and args.full_s2 and not args.no_audit:
        audit_sink = build_pg_enforcement_audit_sink(
            dsn=dsn,
            account_id=args.audit_account_id or args.account_id,
            run_id=args.run_id,
        )

    loaded = None
    repo = None
    if args.system == "S2" and args.full_s2:
        repo = _ConnectionPerSessionRepository(dsn)
        world = _load_world_json(args.world_json) if args.world_json else _default_world(mcp_endpoints, args.agents)
        loaded = asyncio.run(WorldLoader(repo, args.account_id).load(world))

    tool_proxy = None
    if args.tool:
        if args.system == "S1":
            tool_proxy = build_s1_tool_proxy(
                mode=args.mode,
                endpoint_config=McpEndpointConfig.from_mapping(mcp_endpoints),
                decision_sink=sink,
                account_id=args.account_id,
            )
        elif args.full_s2:
            assert repo is not None and loaded is not None
            tool_proxy = build_full_s2_tool_proxy(
                repo=repo,
                loaded=loaded,
                account_id=args.account_id,
                mode=args.mode,
                endpoint_config=McpEndpointConfig.from_mapping(mcp_endpoints),
                decision_sink=sink,
                audit_sink=audit_sink,
            )
        else:
            tool_proxy = build_weak_s2_tool_proxy(
                mode=args.mode,
                endpoint_config=McpEndpointConfig.from_mapping(mcp_endpoints),
                decision_sink=sink,
            )

    handoff_proxy = None
    if args.handoff:
        if args.system == "S1":
            handoff_proxy = build_s1_handoff_proxy(
                mode=args.mode,
                decision_sink=sink,
                account_id=args.account_id,
            )
        elif args.full_s2:
            assert repo is not None and loaded is not None
            handoff_proxy = build_full_s2_handoff_proxy(
                repo=repo,
                loaded=loaded,
                account_id=args.account_id,
                mode=args.mode,
                decision_sink=sink,
                audit_sink=audit_sink,
            )
        else:
            handoff_proxy = build_weak_s2_handoff_proxy(mode=args.mode, decision_sink=sink)

    fleet = start_proxy_fleet(
        deployment,
        tool_proxy=tool_proxy,
        handoff_proxy=handoff_proxy,
        audit_sinks=[audit_sink] if audit_sink is not None else [],
    )
    fleet.wait_ready(timeout_seconds=args.health_timeout_seconds)
    print(
        "READY "
        f"tool_listeners={len(deployment.tool_listeners) if args.tool else 0} "
        f"handoff_listeners={len(deployment.handoff_listeners) if args.handoff else 0} "
        f"mode={args.mode} system={args.system} full_s2={args.full_s2} log={args.log}",
        flush=True,
    )
    print(f"ARTIFACTS={json.dumps(artifacts, sort_keys=True)}", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        fleet.stop()
        print("STOPPED", flush=True)
    return 0


class _ConnectionPerSessionRepository:
    """Loop-safe Postgres repo for the threaded online proxy launcher."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    @asynccontextmanager
    async def session(self, account_id: str):
        conn = await asyncpg.connect(dsn=self._dsn)
        try:
            await init_pg_connection(conn)
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.account_id', $1, true)", account_id)
                yield ScopedRepo(conn)
        finally:
            await conn.close()


def _json_env(payload: Mapping[str, str]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def _yaml_scalar(value: str) -> str:
    if value == "":
        return '""'
    return "'" + value.replace("'", "''") + "'"


def _port_accepts(host: str, port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _load_world_json(path: Path | None) -> Any:
    if path is None:
        raise ValueError("--world-json is required when no default world should be inferred")
    return json.loads(path.read_text(encoding="utf-8"))


def _default_world(mcp_endpoints: Mapping[str, str], agents: Iterable[str]) -> dict[str, Any]:
    return {
        "roles": [
            {"role": agent, "owner_space": _owner_space(agent)}
            for agent in agents
            if agent in ROLE_TO_SERVERS
        ],
        "tool_schemas": [
            {"server": server, "version": 1, "endpoint": endpoint}
            for server, endpoint in sorted(mcp_endpoints.items())
        ],
        "objects": [],
        "policies": [],
    }


def _owner_space(agent: str) -> str:
    from integrations.entcollabbench.mapping import role_to_owner_space

    return role_to_owner_space(agent)


if __name__ == "__main__":
    sys.exit(main())
