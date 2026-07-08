from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass, replace
import re
from typing import Any

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import GuardrailDecision, Verdict
from contexthub.enforcement.guardrails.flow import FlowGuardrail
from contexthub.models.request import RequestContext
from contexthub.services.access_decision import AccessDecision
from integrations.agentleak.decision_log import build_decision_log_record, summarize_flow_payload
from integrations.agentleak.mapping import channel_to_boundary
from integrations.agentleak.trace_schema import (
    AgentLeakChannel,
    AgentLeakTraceEvent,
    CompiledAgentLeakPolicy,
)

_MASK_TOKEN = "[REDACTED]"
_SUPPORTED_CHANNELS = {
    AgentLeakChannel.C2,
    AgentLeakChannel.C3,
    AgentLeakChannel.C5,
    AgentLeakChannel.C6,
    AgentLeakChannel.C7,
}
_PSEUDO_BOUNDARIES = {
    AgentLeakChannel.C6: "log_persistence",
    AgentLeakChannel.C7: "artifact_persistence",
}


@dataclass
class AgentLeakEnforcementResult:
    original_event: AgentLeakTraceEvent
    sanitized_event: AgentLeakTraceEvent | None
    decision: GuardrailDecision
    forwarded: bool
    decision_log: dict[str, Any]
    flow_payload_summary: dict[str, Any]


class PolicyBackedAgentLeakACL:
    """Lightweight ACL backed only by Task 2 compiled AgentLeak policy."""

    def __init__(self, policy: CompiledAgentLeakPolicy):
        self._policy = policy
        self.calls: list[tuple[str, str]] = []

    async def check_read_access(
        self,
        db,
        uri: str,
        ctx: RequestContext,
    ) -> AccessDecision:
        self.calls.append((uri, ctx.agent_id))
        field = _field_for_uri(self._policy, uri)
        if field is None:
            return AccessDecision(
                allowed=False,
                field_masks=None,
                reason="uri not present in compiled AgentLeak policy",
            )

        action = self._policy.field_actions.get(field, "deny")
        if action == "allow":
            return AccessDecision(
                allowed=True,
                field_masks=None,
                reason="compiled AgentLeak policy allows field",
            )
        if action == "mask":
            return AccessDecision(
                allowed=True,
                field_masks=[field],
                reason="compiled AgentLeak policy masks field",
            )
        return AccessDecision(
            allowed=False,
            field_masks=None,
            reason="compiled AgentLeak policy denies field",
        )


class AgentLeakFlowRuntime:
    """AL-S3 adapter from normalized AgentLeak events to FlowGuardrail."""

    def __init__(
        self,
        policy: CompiledAgentLeakPolicy,
        *,
        acl: Any | None = None,
        account_id: str = "agentleak",
        system: str = "AL-S3",
        repair_mode: bool = False,
    ):
        self.policy = policy
        self.acl = acl or PolicyBackedAgentLeakACL(policy)
        self.account_id = account_id
        self.system = system
        self.repair_mode = repair_mode
        self.guardrail = FlowGuardrail(self.acl)

    async def enforce_event(
        self,
        event: AgentLeakTraceEvent,
        *,
        db=None,
    ) -> AgentLeakEnforcementResult:
        boundary = _enforcement_boundary(event.channel)
        protocol_boundary = _protocol_boundary(event.channel, boundary)
        flow_payload, semantic_unmapped = self._flow_payload(event)
        event_for_decision = _event_with_runtime_metadata(
            event,
            semantic_unmapped=semantic_unmapped,
            flow_payload_summary=summarize_flow_payload(flow_payload),
        )

        if boundary is None:
            decision = GuardrailDecision(
                verdict=Verdict.ALLOW,
                reason="channel not flow-enforced by AL-S3",
                guardrail="flow",
            )
            sanitized_event = event_for_decision
            forwarded = True
            log_boundary = Boundary.INVOCATION
        else:
            ec = EnforcementContext(
                boundary=boundary,
                actor=_request_context(event.actor or event.source or "unknown", self.account_id),
                recipient=_recipient_context(event, self.account_id),
                payload=flow_payload,
                declared_context_uris=[item["uri"] for item in flow_payload["items"]],
                workflow_id=event.trace_id,
            )
            decision = await self.guardrail.check(db, ec)
            sanitized_event, forwarded = self._apply_decision(event_for_decision, decision)
            log_boundary = boundary

        decision_log = build_decision_log_record(
            event=event_for_decision,
            decision=decision,
            boundary=log_boundary,
            protocol_boundary=protocol_boundary,
            system=self.system,
        )
        if sanitized_event is not None:
            sanitized_event = replace(
                sanitized_event,
                contexthub_decision_ref=decision_log["decision_id"],
            )
        return AgentLeakEnforcementResult(
            original_event=event,
            sanitized_event=sanitized_event,
            decision=decision,
            forwarded=forwarded,
            decision_log=decision_log,
            flow_payload_summary=summarize_flow_payload(flow_payload),
        )

    def _flow_payload(self, event: AgentLeakTraceEvent) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        items: list[dict[str, Any]] = []
        selected_fields = _fields_from_event(event)
        for field in sorted(selected_fields):
            uri = self.policy.uri_by_field.get(field)
            if uri is None:
                continue
            items.append(
                {
                    "uri": uri,
                    "fields": {field: _field_value(event, self.policy, field)},
                }
            )

        semantic_unmapped = (
            event.channel in _SUPPORTED_CHANNELS
            and not items
            and isinstance(event.content, str)
            and bool(event.content.strip())
        )
        return {"items": items}, semantic_unmapped

    def _apply_decision(
        self,
        event: AgentLeakTraceEvent,
        decision: GuardrailDecision,
    ) -> tuple[AgentLeakTraceEvent | None, bool]:
        if decision.verdict == Verdict.ALLOW:
            return event, True
        if self.repair_mode:
            # Field-level text redaction: remove unauthorized field VALUES from
            # the content text and forward, instead of blocking the whole
            # message. A would-be BLOCK is reclassified as REPAIR so the log and
            # metrics reflect that the message was forwarded after repair.
            repaired = _repair_event_text(event, self.policy)
            if decision.verdict in {Verdict.BLOCK, Verdict.ESCALATE}:
                decision.verdict = Verdict.REPAIR
            return repaired, True
        if decision.verdict in {Verdict.BLOCK, Verdict.ESCALATE}:
            return None, False
        if decision.verdict == Verdict.REPAIR:
            return _repair_event(event, decision.sanitized_payload), True
        return None, False


def _enforcement_boundary(channel: AgentLeakChannel | str) -> Boundary | None:
    normalized = AgentLeakChannel(channel)
    boundary = channel_to_boundary(normalized)
    if boundary is not None:
        return boundary
    if normalized in _PSEUDO_BOUNDARIES:
        # Core ContextHub has no log/artifact boundary yet. Use FlowGuardrail's
        # invocation-capable check while preserving protocol boundary in logs.
        return Boundary.INVOCATION
    return None


def _protocol_boundary(channel: AgentLeakChannel | str, boundary: Boundary | None) -> str | None:
    normalized = AgentLeakChannel(channel)
    if normalized in _PSEUDO_BOUNDARIES:
        return _PSEUDO_BOUNDARIES[normalized]
    return boundary.value if boundary is not None else None


def _request_context(agent_id: str, account_id: str) -> RequestContext:
    return RequestContext(account_id=account_id, agent_id=agent_id)


def _recipient_context(event: AgentLeakTraceEvent, account_id: str) -> RequestContext | None:
    subject = event.recipient or event.target
    if subject is None:
        return None
    return _request_context(subject, account_id)


def _fields_from_event(event: AgentLeakTraceEvent) -> set[str]:
    fields: set[str] = set()
    for item in event.flow_items:
        names = item.get("field_names")
        if isinstance(names, list):
            fields.update(str(name) for name in names)
    if fields:
        return fields
    return {str(field) for field in event.vault}


def _field_value(
    event: AgentLeakTraceEvent,
    policy: CompiledAgentLeakPolicy,
    field: str,
) -> Any:
    if isinstance(event.content, Mapping) and field in event.content:
        return event.content[field]
    if field in event.vault:
        return event.vault[field]
    return policy.field_values.get(field)


def _field_for_uri(policy: CompiledAgentLeakPolicy, uri: str) -> str | None:
    for field, candidate in policy.uri_by_field.items():
        if candidate == uri:
            return field
    return None


def _repair_event(
    event: AgentLeakTraceEvent,
    sanitized_payload: dict | None,
) -> AgentLeakTraceEvent:
    repaired_content = _repair_content(event.content, sanitized_payload)
    repaired_vault = _repair_mapping(event.vault, sanitized_payload)
    metadata = {
        **event.metadata,
        "contexthub_flow": {
            **dict(event.metadata.get("contexthub_flow") or {}),
            "sanitized_payload_summary": summarize_flow_payload(sanitized_payload),
        },
    }
    return replace(event, content=repaired_content, vault=repaired_vault, metadata=metadata)


def _repair_content(content: str | dict[str, Any], sanitized_payload: dict | None) -> str | dict[str, Any]:
    if not isinstance(content, Mapping):
        return content
    return _repair_mapping(dict(content), sanitized_payload)


def _repair_mapping(payload: Mapping[str, Any], sanitized_payload: dict | None) -> dict[str, Any]:
    repaired = copy.deepcopy(dict(payload))
    for field, value in _sanitized_field_values(sanitized_payload).items():
        if field in repaired:
            repaired[field] = value
    return repaired


def _sanitized_field_values(sanitized_payload: dict | None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if not isinstance(sanitized_payload, Mapping):
        return values
    items = sanitized_payload.get("items")
    if not isinstance(items, list):
        return values
    for item in items:
        if not isinstance(item, Mapping):
            continue
        fields = item.get("fields")
        if isinstance(fields, Mapping):
            values.update(dict(fields))
    return values


def _unauthorized_value_fields(policy: CompiledAgentLeakPolicy) -> list[str]:
    """Fields whose raw value must be redacted from content under repair.

    Same basis as run_eval._structured_leaked_fields (the leak metric): a field
    is unauthorized iff neither its compiled key nor its raw name is allowed,
    and its value is longer than 3 chars. The request-text exclusion the metric
    applies is intentionally NOT mirrored here — the repair runtime does not
    receive the user request, so any over-redaction of user-provided values is
    surfaced honestly via the utility signal rather than hidden.
    """

    raw_name_by_field = policy.metadata.get("raw_name_by_field", {})
    fields: list[str] = []
    for field in policy.field_values:
        if field in policy.allowed_fields:
            continue
        if raw_name_by_field.get(field, field) in policy.allowed_fields:
            continue
        value = policy.field_values.get(field)
        if value is None or len(str(value)) <= 3:
            continue
        fields.append(field)
    return fields


def _redact_text(text: str, policy: CompiledAgentLeakPolicy) -> str:
    """Case-insensitively remove unauthorized field values from free text.

    Values are replaced longest-first so a value that is a substring of another
    is not partially clobbered. Matching is case-insensitive because the leak
    metric lowercases both sides before comparing.
    """

    values = sorted(
        {str(policy.field_values[field]) for field in _unauthorized_value_fields(policy)},
        key=len,
        reverse=True,
    )
    redacted = text
    for value in values:
        if not value:
            continue
        redacted = re.sub(re.escape(value), _MASK_TOKEN, redacted, flags=re.IGNORECASE)
    return redacted


def _redact_value(value: Any, policy: CompiledAgentLeakPolicy) -> Any:
    if isinstance(value, str):
        return _redact_text(value, policy)
    if isinstance(value, Mapping):
        return {key: _redact_value(item, policy) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, policy) for item in value]
    return value


def _repair_event_text(
    event: AgentLeakTraceEvent,
    policy: CompiledAgentLeakPolicy,
) -> AgentLeakTraceEvent:
    """Forward the event with unauthorized field values stripped from content.

    String content is redacted by case-insensitive value match; mapping content
    is redacted recursively. The vault is not modified (it is the policy oracle,
    not transmitted content).
    """

    repaired_content = _redact_value(event.content, policy)
    metadata = {
        **event.metadata,
        "contexthub_flow": {
            **dict(event.metadata.get("contexthub_flow") or {}),
            "repair_mode": "field_level_text_redaction",
        },
    }
    return replace(event, content=repaired_content, metadata=metadata)


def _event_with_runtime_metadata(
    event: AgentLeakTraceEvent,
    *,
    semantic_unmapped: bool,
    flow_payload_summary: dict[str, Any],
) -> AgentLeakTraceEvent:
    metadata = {
        **event.metadata,
        "semantic_unmapped": semantic_unmapped,
        "contexthub_flow": {
            **dict(event.metadata.get("contexthub_flow") or {}),
            "payload_summary": flow_payload_summary,
            "uses_online_llm_or_detector": False,
        },
    }
    return replace(event, metadata=metadata)


__all__ = [
    "AgentLeakEnforcementResult",
    "AgentLeakFlowRuntime",
    "PolicyBackedAgentLeakACL",
]
