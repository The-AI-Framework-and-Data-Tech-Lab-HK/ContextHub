"""ContextHub online handoff (agent-to-agent) reverse proxy for EntCollabBench.

Phase 4 P1-5: the **真在线 pre-dispatch** handoff gate. EntCollabBench delegation
is a structured HTTP tool (``ask_{target}_by_http`` → ``POST /v1/agent/tasks`` on
the peer agent). This proxy sits in front of the *recipient* agent service and
runs the ContextHub handoff gate *before* the downstream agent ever starts:

- ``observe`` mode: compute the verdict, write a decision record, but **always
  forward** the original delegation. Measure-only (feeds H4 / P1-7).
- ``enforce`` mode: ``allow`` forwards; everything else returns an error task
  response **without ever contacting the downstream agent**, so an incomplete /
  unauthorized handoff is blocked before the peer runs.

Scope is **weak S2** (§1 decision 6 / P0-2b): no DB. The reachable signal is
``incomplete_handoff`` (static packet completeness). Recipient ACL
(``unauthorized_flow``) and stale/blocked context refs need DB + a loaded world,
so they are honestly **unreachable here** (NoopACL + NoopStaleness) and deferred
to P0-2b. The handoff packet's ContextHub fields are not part of the
EntCollabBench wire payload, so deterministic repair cannot be injected into the
delegation; non-allow verdicts therefore block the delegation in enforce mode.

Identity is option A: one listener per recipient agent (port fixes the
recipient); the sender is read from the delegation payload's ``source_agent``.
The exact ``/v1/agent/tasks`` response schema depends on EntCollabBench
``api_schema`` and should be confirmed against a live service (Docker) before the
enforce flip; the synthesized block response mirrors the documented
delegate-error shape (``status``/``error``/``result_preview``).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import threading
import time
from typing import Any

from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.service import EnforcementService
from contexthub.models.request import RequestContext
from contexthub.services.access_decision import AccessDecision

from integrations.entcollabbench import mapping
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
    would_block_action,
)
from integrations.entcollabbench.runtime_wrapper import (
    ContextHubRuntimeWrapper,
    NoopStaleness,
    RuntimeHandoffEnforcementResult,
    build_full_s2_runtime_wrapper,
    build_s1_runtime_wrapper,
)
from integrations.entcollabbench.s2_diagnostic import extract_object_ids
from integrations.entcollabbench.world_loader import LoadedWorld, WorldLoader

__all__ = [
    "ENFORCE",
    "ENTCOLLAB_API_VERSION",
    "OBSERVE",
    "AgentHandoffProxy",
    "ForwardResponse",
    "HandoffDecisionRecord",
    "HandoffProxyRoute",
    "JsonlDecisionSink",
    "ProxyOutcome",
    "build_full_s2_handoff_proxy",
    "build_full_s2_handoff_proxy_from_world",
    "build_handoff_packet",
    "build_s1_handoff_proxy",
    "build_weak_s2_handoff_proxy",
]

# Mirrors EntCollabBench api_schema.API_VERSION at pinned commit 9d085fcb. The
# delegating agent runs api_schema.parse_task_response on our reply, which raises
# without a valid api_version / handled_by / recursion, so a block response must
# be a well-formed AgentTaskResponse. status="rejected" makes the caller's
# ask_{agent}_by_http tool return "Error: {error}" and log delegate_non_ok, while
# the downstream agent never runs. Re-verify this constant if the source moves.
ENTCOLLAB_API_VERSION = "2026-04-04"

# Backward-compatible alias: the generic listener serves the handoff proxy too.
HandoffProxyHTTPServer = ProxyHTTPServer


@dataclass(frozen=True)
class HandoffProxyRoute:
    """A single listener identity → downstream recipient agent service.

    ``target_agent`` (the recipient) is fixed per listener (option A: identity by
    port); the sender is read from the delegation payload.
    """

    target_agent: str
    upstream_endpoint: str


@dataclass
class HandoffDecisionRecord:
    """One handoff decision, the unit of false-block analysis in observe mode."""

    ts: float
    agent_id: str  # sender, for uniform aggregation with tool records
    recipient: str
    mode: str
    verdict: str
    action: str
    allow: bool
    would_block: bool
    would_repair: bool
    forwarded: bool
    schema_source: str
    reason: str
    request_id: Any = None
    missing_fields: list[str] = field(default_factory=list)
    required_object_ids: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "agent_id": self.agent_id,
            "recipient": self.recipient,
            "boundary": "handoff",
            "mode": self.mode,
            "verdict": self.verdict,
            "action": self.action,
            "allow": self.allow,
            "would_block": self.would_block,
            "would_repair": self.would_repair,
            "forwarded": self.forwarded,
            "schema_source": self.schema_source,
            "reason": self.reason,
            "request_id": self.request_id,
            "missing_fields": self.missing_fields,
            "required_object_ids": self.required_object_ids,
            "violations": self.violations,
            "error": self.error,
        }


class NoopACL:
    """Weak-S2 ACL: no DB, so recipient read-access is always allowed.

    This makes ``unauthorized_flow`` honestly unreachable in weak S2 (deferred to
    P0-2b), mirroring NoopStaleness on the tool side.
    """

    async def check_read_access(self, db: Any, uri: str, ctx: RequestContext) -> AccessDecision:
        return AccessDecision(allowed=True, field_masks=None, reason="weak-s2 noop acl")


class AgentHandoffProxy:
    """Protocol-agnostic core: gate a delegation, then forward or block."""

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
        self.records: list[HandoffDecisionRecord] = []

    @property
    def mode(self) -> str:
        return self._mode

    async def handle(
        self,
        route: HandoffProxyRoute,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ProxyOutcome:
        payload = try_parse_json(body)

        # Only gate agent-to-agent delegations. The entry task (benchmark →
        # entry agent) has no ``source_agent`` and must pass through ungated.
        if not isinstance(payload, dict) or not payload.get("source_agent"):
            return ProxyOutcome(await self._forward(route, headers, body), None)

        sender = str(payload.get("source_agent") or "")
        recipient = route.target_agent or str(payload.get("target_agent") or "")
        packet = build_handoff_packet(payload, recipient=recipient)

        try:
            result = await self._wrapper.enforce_handoff_before_delegate(
                sender=sender,
                recipient=recipient,
                packet=packet,
            )
        except Exception as exc:  # fail-open: never let the gate break the run
            record = self._error_record(sender, recipient, payload, packet, exc)
            self._emit(record)
            self._audit(record)
            return ProxyOutcome(await self._forward(route, headers, body), record)

        response, forwarded = await self._act(route, headers, body, payload, sender, recipient, result)
        record = self._record_for(
            sender, recipient, payload, packet, result, forwarded=forwarded
        )
        self._emit(record)
        self._audit(record)
        return ProxyOutcome(response, record)

    async def _act(
        self,
        route: HandoffProxyRoute,
        headers: Mapping[str, str],
        body: bytes,
        payload: dict[str, Any],
        sender: str,
        recipient: str,
        result: RuntimeHandoffEnforcementResult,
    ) -> tuple[ForwardResponse, bool]:
        # Observe never alters the outcome: always forward the original.
        if self._mode == OBSERVE:
            return await self._forward(route, headers, body), True

        if result.action.allow:
            return await self._forward(route, headers, body), True

        # Repair cannot be injected into the EntCollabBench wire payload, so any
        # non-allow verdict blocks the delegation before the peer agent runs.
        return _block_response(payload, sender, recipient, result), False

    async def _forward(
        self,
        route: HandoffProxyRoute,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ForwardResponse:
        return await maybe_await(self._upstream(route, relayable_headers(headers), body))

    def _record_for(
        self,
        sender: str,
        recipient: str,
        payload: dict[str, Any],
        packet: Mapping[str, Any],
        result: RuntimeHandoffEnforcementResult,
        *,
        forwarded: bool,
    ) -> HandoffDecisionRecord:
        action = result.action
        return HandoffDecisionRecord(
            ts=self._clock(),
            agent_id=sender,
            recipient=recipient,
            mode=self._mode,
            verdict=verdict_value(result),
            action=action.action,
            allow=bool(action.allow),
            would_block=would_block_action(action.action),
            would_repair=action.action == "retry_with_patch",
            forwarded=forwarded,
            schema_source="delegation-payload",
            reason=result.decision.reason,
            request_id=payload.get("request_id") or payload.get("id"),
            missing_fields=_missing_fields(result),
            required_object_ids=list(packet.get("required_object_ids") or []),
            violations=violations_json(result),
        )

    def _error_record(
        self,
        sender: str,
        recipient: str,
        payload: dict[str, Any],
        packet: Mapping[str, Any],
        exc: Exception,
    ) -> HandoffDecisionRecord:
        return HandoffDecisionRecord(
            ts=self._clock(),
            agent_id=sender,
            recipient=recipient,
            mode=self._mode,
            verdict="error",
            action="error",
            allow=False,
            would_block=False,
            would_repair=False,
            forwarded=True,
            schema_source="gate-error",
            reason=f"{type(exc).__name__}: {exc}",
            request_id=payload.get("request_id") or payload.get("id"),
            required_object_ids=list(packet.get("required_object_ids") or []),
            error=f"{type(exc).__name__}: {exc}",
        )

    def _emit(self, record: HandoffDecisionRecord) -> None:
        with self._lock:
            self.records.append(record)
        if self._sink is not None:
            self._sink(record)

    def _audit(self, record: HandoffDecisionRecord) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(record)
        except Exception:
            # Audit is deliberately best-effort for online proxy decisions.
            return


def build_handoff_packet(payload: Mapping[str, Any], *, recipient: str) -> dict[str, Any]:
    """Translate an EntCollabBench delegation payload into a ContextHub packet.

    Object IDs are best-effort regex extractions from the natural-language task
    text (same heuristic as the post-run diagnostic); this is approximate and is
    a known source of observe-phase would-repair noise to be quantified before
    the enforce flip.
    """

    sender = str(payload.get("source_agent") or "")
    task = str(payload.get("task") or "")
    return {
        "sender": sender,
        "recipient": str(recipient or payload.get("target_agent") or ""),
        "task_intent": task,
        "expected_action": "complete_delegated_task" if task.strip() else "",
        "required_object_ids": extract_object_ids(task),
        "context_versions": [mapping.role_uri(recipient)] if recipient else [],
    }


def build_weak_s2_handoff_proxy(
    *,
    mode: str = OBSERVE,
    decision_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
    loaded: LoadedWorld | None = None,
) -> AgentHandoffProxy:
    """Construct a weak-S2 handoff proxy (NoopACL + NoopStaleness, no DB)."""

    loaded_world = loaded or LoadedWorld()
    staleness = NoopStaleness()
    service = EnforcementService(
        [
            HandoffGuardrail(
                NoopACL(),
                staleness,
                object_uri_resolver=loaded_world.object_uri,
                version_uri_resolver=mapping.resolve_version_tag,
            )
        ]
    )
    wrapper = ContextHubRuntimeWrapper(
        loaded=loaded_world,
        service=service,
        staleness=staleness,
    )
    return AgentHandoffProxy(
        wrapper,
        mode=mode,
        decision_sink=decision_sink,
        upstream=upstream,
    )


def build_s1_handoff_proxy(
    *,
    mode: str = OBSERVE,
    decision_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
    account_id: str = "entcollab-runtime",
) -> AgentHandoffProxy:
    """Construct an online S1 generic-control handoff proxy."""

    return AgentHandoffProxy(
        build_s1_runtime_wrapper(account_id=account_id),
        mode=mode,
        decision_sink=decision_sink,
        upstream=upstream,
    )


def build_full_s2_handoff_proxy(
    *,
    repo: Any,
    loaded: LoadedWorld,
    account_id: str = "entcollab-runtime",
    mode: str = OBSERVE,
    decision_sink: DecisionSink | None = None,
    audit_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
) -> AgentHandoffProxy:
    """Construct a DB-backed S2 handoff proxy with side-channel audit support."""

    wrapper = build_full_s2_runtime_wrapper(
        repo=repo,
        loaded=loaded,
        account_id=account_id,
    )
    return AgentHandoffProxy(
        wrapper,
        mode=mode,
        decision_sink=decision_sink,
        audit_sink=audit_sink,
        upstream=upstream,
    )


async def build_full_s2_handoff_proxy_from_world(
    *,
    repo: Any,
    world: Any,
    account_id: str = "entcollab-runtime",
    mode: str = OBSERVE,
    decision_sink: DecisionSink | None = None,
    audit_sink: DecisionSink | None = None,
    upstream: Callable[..., Any] | None = None,
) -> AgentHandoffProxy:
    """Load an EntCollabBench world into Postgres, then build full S2."""

    loaded = await WorldLoader(repo, account_id).load(world)
    return build_full_s2_handoff_proxy(
        repo=repo,
        loaded=loaded,
        account_id=account_id,
        mode=mode,
        decision_sink=decision_sink,
        audit_sink=audit_sink,
        upstream=upstream,
    )


def _missing_fields(result: RuntimeHandoffEnforcementResult) -> list[str]:
    for violation in result.decision.violations:
        kind = violation.kind.value if hasattr(violation.kind, "value") else str(violation.kind)
        if kind == "incomplete_handoff":
            hint = violation.repair_hint or {}
            missing = hint.get("missing_fields")
            if isinstance(missing, list):
                return [str(item) for item in missing]
    return []


def _block_response(
    payload: dict[str, Any],
    sender: str,
    recipient: str,
    result: RuntimeHandoffEnforcementResult,
) -> ForwardResponse:
    verdict = verdict_value(result)
    reason = result.decision.reason or "blocked by ContextHub S2 handoff enforcement"
    text = f"[ContextHub S2 handoff blocked] verdict={verdict}: {reason}"
    # Calibrated to api_schema.AgentTaskResponse (see ENTCOLLAB_API_VERSION):
    # status="rejected" + populated error is surfaced to the model as
    # "Error: {error}" without the downstream agent ever running.
    body = {
        "request_id": str(payload.get("request_id") or payload.get("id") or "contexthub-blocked"),
        "api_version": ENTCOLLAB_API_VERSION,
        "status": "rejected",
        "handled_by": recipient,
        "result": "",
        "error": text,
        "recursion": _echo_recursion(payload),
        "_contexthub": {
            "blocked": True,
            "verdict": verdict,
            "action": result.action.action,
            "guardrail": result.decision.guardrail,
            "missing_fields": _missing_fields(result),
            "violations": violations_json(result),
        },
    }
    return ForwardResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )


def _echo_recursion(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Echo a valid RecursionMeta from the request, or a safe default.

    api_schema._parse_recursion requires non-negative ints with depth<=max_depth
    and a list[str] trace, else parse_task_response raises.
    """

    rec = payload.get("recursion")
    if isinstance(rec, Mapping):
        depth = rec.get("depth")
        max_depth = rec.get("max_depth")
        trace = rec.get("trace", [])
        if (
            isinstance(depth, int)
            and not isinstance(depth, bool)
            and isinstance(max_depth, int)
            and not isinstance(max_depth, bool)
            and depth >= 0
            and max_depth >= 0
            and depth <= max_depth
            and isinstance(trace, list)
            and all(isinstance(item, str) for item in trace)
        ):
            return {"depth": depth, "max_depth": max_depth, "trace": list(trace)}
    return {"depth": 0, "max_depth": 0, "trace": []}
