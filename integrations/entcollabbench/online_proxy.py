"""ContextHub online MCP tool reverse proxy for EntCollabBench.

This module is the **真在线 pre-dispatch** tool gate for Phase 4 P0-2. It sits as
a standalone reverse proxy in front of each EntCollabBench MCP service. For
JSON-RPC ``tools/call`` requests it runs the ContextHub tool-state gate *before*
the real MCP service executes the tool:

- ``observe`` mode: compute the verdict, write a decision record, but **always
  forward** the original request. The proxy cannot change task outcomes, so it
  only measures would-block / false-block rates (feeds H4 / P1-7).
- ``enforce`` mode: ``allow`` forwards; deterministic repair forwards a patched
  request; everything else returns an MCP ``isError`` result **without ever
  contacting the upstream MCP service**, so the DB mutation never happens.

The weak-S2 builder keeps the old in-memory ``LoadedWorld`` + ``NoopStaleness``
shape for smoke tests. The full-S2 builder wires a real repo/staleness checker
and optional fail-open audit side channel for P0-2b.

Shared proxy scaffolding (modes, ``ForwardResponse``, relay, sink, HTTP server)
lives in ``online_proxy_base`` so the handoff proxy can reuse it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass, field
import json
import threading
import time
from typing import Any

from integrations.entcollabbench.mcp_runtime_adapter import McpEndpointConfig
from integrations.entcollabbench.online_proxy_base import (
    ENFORCE,
    OBSERVE,
    DecisionSink,
    ForwardResponse,
    JsonlDecisionSink,
    ProxyHTTPServer,
    ProxyOutcome,
    default_urllib_forward,
    maybe_await,
    relayable_headers,
    try_parse_json,
    validate_mode,
    verdict_value,
    violations_json,
    without_content_length,
    would_block_action,
)
from integrations.entcollabbench.runtime_wrapper import (
    ContextHubRuntimeWrapper,
    RuntimeToolEnforcementResult,
    build_full_s2_runtime_wrapper,
    build_s1_runtime_wrapper,
)
from integrations.entcollabbench.world_loader import LoadedWorld, WorldLoader

__all__ = [
    "ENFORCE",
    "OBSERVE",
    "DecisionRecord",
    "ForwardResponse",
    "JsonlDecisionSink",
    "McpToolProxy",
    "ProxyOutcome",
    "ToolProxyHTTPServer",
    "ToolProxyRoute",
    "build_full_s2_tool_proxy",
    "build_full_s2_tool_proxy_from_world",
    "build_s1_tool_proxy",
    "build_weak_s2_tool_proxy",
]

SchemaProvider = Callable[[str, str], Mapping[str, Any]]

# Backward-compatible alias: the generic listener serves the tool proxy too.
ToolProxyHTTPServer = ProxyHTTPServer


@dataclass(frozen=True)
class ToolProxyRoute:
    """A single listener identity → upstream MCP service.

    ``agent_id`` is fixed per listener (option A: identity by port), so the
    proxy never has to reverse-map the per-agent MCP auth header.
    """

    agent_id: str
    server: str
    upstream_endpoint: str


@dataclass
class DecisionRecord:
    """One tool-call decision, the unit of false-block analysis in observe mode."""

    ts: float
    agent_id: str
    server: str
    tool_name: str
    mode: str
    verdict: str
    action: str
    allow: bool
    would_block: bool
    would_repair: bool
    forwarded: bool
    patched: bool
    schema_source: str
    reason: str
    request_id: Any = None
    violations: list[dict[str, Any]] = field(default_factory=list)
    normalized_arg_keys: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "agent_id": self.agent_id,
            "server": self.server,
            "tool_name": self.tool_name,
            "mode": self.mode,
            "verdict": self.verdict,
            "action": self.action,
            "allow": self.allow,
            "would_block": self.would_block,
            "would_repair": self.would_repair,
            "forwarded": self.forwarded,
            "patched": self.patched,
            "schema_source": self.schema_source,
            "reason": self.reason,
            "request_id": self.request_id,
            "violations": self.violations,
            "normalized_arg_keys": self.normalized_arg_keys,
            "error": self.error,
        }


class McpToolProxy:
    """Protocol-agnostic core: gate a JSON-RPC request, then forward or block.

    The HTTP listener is a thin wrapper around :meth:`handle`; tests drive
    :meth:`handle` directly with a fake ``upstream`` and injected schema.
    """

    def __init__(
        self,
        wrapper: ContextHubRuntimeWrapper,
        *,
        mode: str = OBSERVE,
        decision_sink: DecisionSink | None = None,
        audit_sink: DecisionSink | None = None,
        upstream: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._wrapper = wrapper
        self._mode = validate_mode(mode)
        self._sink = decision_sink
        self._audit_sink = audit_sink
        self._upstream = upstream or default_urllib_forward
        self._clock = clock
        self._lock = threading.Lock()
        self.records: list[DecisionRecord] = []

    @property
    def mode(self) -> str:
        return self._mode

    async def handle(
        self,
        route: ToolProxyRoute,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ProxyOutcome:
        payload = try_parse_json(body)

        # JSON-RPC batches can smuggle a tools/call past a naive method check.
        # We cannot safely gate each element of a batch response yet, so we
        # close the bypass conservatively instead of silently forwarding.
        if isinstance(payload, list):
            return await self._handle_batch(route, headers, body, payload)

        if not _is_tools_call(payload):
            # Transparent passthrough for initialize / tools/list / notifications.
            return ProxyOutcome(await self._forward(route, headers, body), None)

        params = payload.get("params") or {}
        tool_name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        if not isinstance(raw_args, Mapping):
            raw_args = {}

        try:
            result = await self._wrapper.enforce_tool_call_before_execute(
                agent_id=route.agent_id,
                server=route.server,
                tool_name=tool_name,
                raw_args=raw_args,
            )
        except Exception as exc:  # fail-open: never let the gate break the run
            record = self._error_record(route, tool_name, payload, exc)
            self._emit(record)
            self._audit(record)
            return ProxyOutcome(await self._forward(route, headers, body), record)

        response, forwarded, patched = await self._act(route, headers, body, payload, result)
        record = self._record_for(
            route, tool_name, payload, result, forwarded=forwarded, patched=patched
        )
        self._emit(record)
        self._audit(record)
        return ProxyOutcome(response, record)

    async def _handle_batch(
        self,
        route: ToolProxyRoute,
        headers: Mapping[str, str],
        body: bytes,
        payload: list[Any],
    ) -> ProxyOutcome:
        has_tools_call = any(
            isinstance(item, Mapping) and item.get("method") == "tools/call"
            for item in payload
        )
        if not has_tools_call:
            # No gated method in the batch: nothing to enforce, forward as-is.
            return ProxyOutcome(await self._forward(route, headers, body), None)

        record = self._batch_record(route, forwarded=self._mode == OBSERVE)
        self._emit(record)
        self._audit(record)
        if self._mode == OBSERVE:
            return ProxyOutcome(await self._forward(route, headers, body), record)
        return ProxyOutcome(_batch_block_response(), record)

    async def _act(
        self,
        route: ToolProxyRoute,
        headers: Mapping[str, str],
        body: bytes,
        payload: dict[str, Any],
        result: RuntimeToolEnforcementResult,
    ) -> tuple[ForwardResponse, bool, bool]:
        action = result.action

        # Observe mode never alters the outcome: always forward the original.
        if self._mode == OBSERVE:
            return await self._forward(route, headers, body), True, False

        if action.allow:
            return await self._forward(route, headers, body), True, False

        if action.action == "retry_with_patch" and action.patch:
            patched_body = _apply_patch(payload, result.normalized_args, action.patch)
            patched_headers = without_content_length(headers)
            return await self._forward(route, patched_headers, patched_body), True, True

        # block / pending(escalate) / retry_with_feedback → synthesize MCP error,
        # upstream is never contacted so the side effect never happens.
        return _block_response(payload, result), False, False

    async def _forward(
        self,
        route: ToolProxyRoute,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ForwardResponse:
        return await maybe_await(self._upstream(route, relayable_headers(headers), body))

    def _record_for(
        self,
        route: ToolProxyRoute,
        tool_name: str,
        payload: dict[str, Any],
        result: RuntimeToolEnforcementResult,
        *,
        forwarded: bool,
        patched: bool,
    ) -> DecisionRecord:
        action = result.action
        return DecisionRecord(
            ts=self._clock(),
            agent_id=route.agent_id,
            server=route.server,
            tool_name=tool_name,
            mode=self._mode,
            verdict=verdict_value(result),
            action=action.action,
            allow=bool(action.allow),
            would_block=would_block_action(action.action),
            would_repair=action.action == "retry_with_patch",
            forwarded=forwarded,
            patched=patched,
            schema_source=result.schema_source,
            reason=result.decision.reason,
            request_id=payload.get("id"),
            violations=violations_json(result),
            normalized_arg_keys=sorted(str(key) for key in result.normalized_args.keys()),
        )

    def _error_record(
        self,
        route: ToolProxyRoute,
        tool_name: str,
        payload: dict[str, Any],
        exc: Exception,
    ) -> DecisionRecord:
        return DecisionRecord(
            ts=self._clock(),
            agent_id=route.agent_id,
            server=route.server,
            tool_name=tool_name,
            mode=self._mode,
            verdict="error",
            action="error",
            allow=False,
            would_block=False,
            would_repair=False,
            forwarded=True,
            patched=False,
            schema_source="gate-error",
            reason=f"{type(exc).__name__}: {exc}",
            request_id=payload.get("id"),
            error=f"{type(exc).__name__}: {exc}",
        )

    def _batch_record(self, route: ToolProxyRoute, *, forwarded: bool) -> DecisionRecord:
        return DecisionRecord(
            ts=self._clock(),
            agent_id=route.agent_id,
            server=route.server,
            tool_name="<batch>",
            mode=self._mode,
            verdict="batch_unsupported",
            action="block" if self._mode == ENFORCE else "observe_forward",
            allow=False,
            would_block=self._mode == ENFORCE,
            would_repair=False,
            forwarded=forwarded,
            patched=False,
            schema_source="batch-unsupported",
            reason="JSON-RPC batch containing tools/call is not individually gated",
        )

    def _emit(self, record: DecisionRecord) -> None:
        with self._lock:
            self.records.append(record)
        if self._sink is not None:
            self._sink(record)

    def _audit(self, record: DecisionRecord) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(record)
        except Exception:
            # Audit is intentionally fail-open and out-of-band for the proxy.
            return


def build_weak_s2_tool_proxy(
    *,
    mode: str = OBSERVE,
    endpoint_config: McpEndpointConfig | None = None,
    schema_provider: SchemaProvider | None = None,
    decision_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
) -> McpToolProxy:
    """Construct a weak-S2 tool proxy (in-memory world, NoopStaleness, no DB)."""

    wrapper = ContextHubRuntimeWrapper(
        schema_provider=schema_provider,
        endpoint_config=endpoint_config,
    )
    return McpToolProxy(
        wrapper,
        mode=mode,
        decision_sink=decision_sink,
        upstream=upstream,
    )


def build_s1_tool_proxy(
    *,
    mode: str = OBSERVE,
    endpoint_config: McpEndpointConfig | None = None,
    schema_provider: SchemaProvider | None = None,
    decision_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
    account_id: str = "entcollab-runtime",
) -> McpToolProxy:
    """Construct an online S1 generic-control tool proxy."""

    wrapper = build_s1_runtime_wrapper(
        account_id=account_id,
        schema_provider=schema_provider,
        endpoint_config=endpoint_config,
    )
    return McpToolProxy(
        wrapper,
        mode=mode,
        decision_sink=decision_sink,
        upstream=upstream,
    )


def build_full_s2_tool_proxy(
    *,
    repo: Any,
    loaded: LoadedWorld,
    account_id: str = "entcollab-runtime",
    mode: str = OBSERVE,
    endpoint_config: McpEndpointConfig | None = None,
    schema_provider: SchemaProvider | None = None,
    decision_sink: DecisionSink | None = None,
    audit_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
) -> McpToolProxy:
    """Construct a DB-backed S2 tool proxy with side-channel audit support."""

    wrapper = build_full_s2_runtime_wrapper(
        repo=repo,
        loaded=loaded,
        account_id=account_id,
        schema_provider=schema_provider,
        endpoint_config=endpoint_config,
    )
    return McpToolProxy(
        wrapper,
        mode=mode,
        decision_sink=decision_sink,
        audit_sink=audit_sink,
        upstream=upstream,
    )


async def build_full_s2_tool_proxy_from_world(
    *,
    repo: Any,
    world: Any,
    account_id: str = "entcollab-runtime",
    mode: str = OBSERVE,
    endpoint_config: McpEndpointConfig | None = None,
    schema_provider: SchemaProvider | None = None,
    decision_sink: DecisionSink | None = None,
    audit_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
) -> McpToolProxy:
    """Load an EntCollabBench world into Postgres, then build full S2."""

    loaded = await WorldLoader(repo, account_id).load(world)
    return build_full_s2_tool_proxy(
        repo=repo,
        loaded=loaded,
        account_id=account_id,
        mode=mode,
        endpoint_config=endpoint_config,
        schema_provider=schema_provider,
        decision_sink=decision_sink,
        audit_sink=audit_sink,
        upstream=upstream,
    )


def _is_tools_call(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("method") == "tools/call"


def _apply_patch(
    payload: dict[str, Any],
    normalized_args: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> bytes:
    new_payload = copy.deepcopy(payload)
    params = new_payload.get("params")
    if not isinstance(params, dict):
        params = {}
        new_payload["params"] = params
    params["arguments"] = {**dict(normalized_args), **dict(patch)}
    return json.dumps(new_payload).encode("utf-8")


def _block_response(
    payload: dict[str, Any],
    result: RuntimeToolEnforcementResult,
) -> ForwardResponse:
    verdict = verdict_value(result)
    reason = result.decision.reason or "blocked by ContextHub S2 enforcement"
    text = f"[ContextHub S2 blocked] verdict={verdict}: {reason}"
    body = {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": True,
            "_contexthub": {
                "blocked": True,
                "verdict": verdict,
                "action": result.action.action,
                "guardrail": result.decision.guardrail,
                "violations": violations_json(result),
            },
        },
    }
    return ForwardResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )


def _batch_block_response() -> ForwardResponse:
    body = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": -32600,
            "message": (
                "[ContextHub S2] batched tools/call is not supported by the online "
                "gate; blocked in enforce mode to avoid an ungated bypass"
            ),
        },
    }
    return ForwardResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )
