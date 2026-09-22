"""Throwaway launcher: bring up the weak-S2 observe tool-proxy fleet for a smoke.

Not part of the shipped surface. It wires the existing pieces
(``proxy_config.build_deployment`` + ``online_proxy``) into a running host-side
fleet so cases 145/146 can be driven through the proxy with ``benchmark.py``:

- upstream = host view of the real MCP services (``mcp_endpoints_export.json``,
  i.e. ``127.0.0.1:<pub>/mcp``), reachable from the host.
- reach    = ``host.docker.internal:<port>/mcp``, what the agent containers POST to.
- mode     = observe (always forwards; only records decisions). No DB, no audit.

It writes the per-agent ``mcp_endpoints.<agent>.json`` files for the operator to
``docker cp`` in, prints a READY line, then serves until SIGINT/SIGTERM.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import asyncpg

from contexthub.config import Settings
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import ScopedRepo
from integrations.entcollabbench.mapping import ROLE_TO_SERVERS
from integrations.entcollabbench.mcp_runtime_adapter import McpEndpointConfig
from integrations.entcollabbench.online_audit import build_pg_enforcement_audit_sink
from integrations.entcollabbench.online_proxy import (
    ToolProxyHTTPServer,
    build_full_s2_tool_proxy,
    build_weak_s2_tool_proxy,
)
from integrations.entcollabbench.proxy_config import build_deployment, write_deployment
from integrations.entcollabbench.world_loader import WorldLoader


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-map", type=Path, required=True, help="mcp_endpoints_export.json (host view)")
    parser.add_argument("--agents", nargs="+", required=True, help="agents to front with a proxy")
    parser.add_argument("--log", type=Path, required=True, help="decision-log JSONL path")
    parser.add_argument("--out-dir", type=Path, required=True, help="where to write per-agent endpoints files")
    parser.add_argument("--base-port", type=int, default=19000)
    parser.add_argument("--mode", choices=["observe", "enforce"], default="observe")
    parser.add_argument("--full-s2", action="store_true", help="use DB-backed staleness and world load")
    parser.add_argument("--world-json", type=Path, help="world metadata JSON for WorldLoader")
    parser.add_argument("--database-url", default=None, help="asyncpg/Postgres DSN")
    parser.add_argument("--account-id", default="entcollab-runtime")
    parser.add_argument("--audit-account-id", default=None)
    parser.add_argument("--run-id", default="entcollab-online-smoke")
    parser.add_argument("--no-audit", action="store_true", help="disable side-channel audit_log writes")
    args = parser.parse_args()

    dsn = args.database_url or Settings().asyncpg_database_url
    export_map = json.loads(args.export_map.read_text(encoding="utf-8"))
    role_to_servers = {a: ROLE_TO_SERVERS[a] for a in args.agents if a in ROLE_TO_SERVERS}

    deployment = build_deployment(
        mcp_endpoints=export_map,
        agent_peers={},  # tool-only smoke; handoff peers are docker-network-internal
        role_to_servers=role_to_servers,
        base_port=args.base_port,
    )
    artifacts = write_deployment(deployment, args.out_dir)

    audit_sink = None
    if args.full_s2 and not args.no_audit:
        audit_sink = build_pg_enforcement_audit_sink(
            dsn=dsn,
            account_id=args.audit_account_id or args.account_id,
            run_id=args.run_id,
        )

    if args.full_s2:
        repo = _ConnectionPerSessionRepository(dsn)
        world = _load_world_json(args.world_json) if args.world_json else _default_world(export_map, args.agents)
        loaded = asyncio.run(WorldLoader(repo, args.account_id).load(world))
        proxy = build_full_s2_tool_proxy(
            repo=repo,
            loaded=loaded,
            account_id=args.account_id,
            mode=args.mode,
            endpoint_config=McpEndpointConfig.from_mapping(export_map),
            decision_sink=_JsonlSink(args.log),
            audit_sink=audit_sink,
        )
    else:
        proxy = build_weak_s2_tool_proxy(
            mode=args.mode,
            endpoint_config=McpEndpointConfig.from_mapping(export_map),
            decision_sink=_JsonlSink(args.log),
        )

    servers: list[ToolProxyHTTPServer] = []
    for listener in deployment.tool_listeners:
        srv = ToolProxyHTTPServer(
            listener.route, proxy, host=listener.bind_host, port=listener.port
        ).start()
        servers.append(srv)

    print(
        f"READY listeners={len(servers)} mode={args.mode} full_s2={args.full_s2} log={args.log}",
        flush=True,
    )
    print(f"ARTIFACTS={json.dumps(artifacts)}", flush=True)
    for listener in deployment.tool_listeners:
        print(
            f"ROUTE {listener.route.agent_id} {listener.route.server} "
            f"reach={listener.reach_url} upstream={listener.route.upstream_endpoint}",
            flush=True,
        )

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()

    for srv in servers:
        srv.stop()
    if audit_sink is not None:
        audit_sink.close(drain=True)
    print(f"STOPPED records={len(proxy.records)}", flush=True)
    return 0


class _JsonlSink:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, record) -> None:
        line = json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


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


def _load_world_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _default_world(export_map: dict[str, str], agents: list[str]) -> dict[str, Any]:
    return {
        "roles": [
            {"role": agent, "owner_space": _owner_space(agent)}
            for agent in agents
            if agent in ROLE_TO_SERVERS
        ],
        "tool_schemas": [
            {"server": server, "version": 1, "endpoint": endpoint}
            for server, endpoint in sorted(export_map.items())
        ],
        "objects": [],
        "policies": [],
    }


def _owner_space(agent: str) -> str:
    from integrations.entcollabbench.mapping import role_to_owner_space

    return role_to_owner_space(agent)


if __name__ == "__main__":
    sys.exit(main())
