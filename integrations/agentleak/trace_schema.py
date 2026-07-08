from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentLeakChannel(StrEnum):
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    C5 = "C5"
    C6 = "C6"
    C7 = "C7"


class AgentLeakEventType(StrEnum):
    MESSAGE_OUT = "message_out"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MEMORY_WRITE = "memory_write"
    LOG_EVENT = "log_event"
    ARTIFACT_WRITE = "artifact_write"


@dataclass
class AgentLeakTraceEvent:
    """Normalized AgentLeak channel event consumed by later Phase 5 tasks.

    ``leaked`` and ``leakage_labels`` are post-hoc evaluation fields. Task 2 does
    not infer them from free text or call any AgentLeak detector.
    """

    trace_id: str
    scenario_id: str
    channel: AgentLeakChannel
    actor: str | None
    recipient: str | None
    content: str | dict[str, Any]
    vault: dict[str, Any]
    allowed_fields: set[str]
    policy_id: str | None = None
    leaked: bool | None = None
    leakage_labels: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str = "fixture-run"
    system: str = "unknown-system"
    model: str = "unknown-model"
    event_type: AgentLeakEventType | str | None = None
    source: str | None = None
    target: str | None = None
    content_ref: str | None = None
    flow_items: list[dict[str, Any]] = field(default_factory=list)
    agentleak_eval: dict[str, Any] = field(default_factory=dict)
    contexthub_decision_ref: str | None = None

    def __post_init__(self) -> None:
        self.trace_id = str(self.trace_id)
        self.scenario_id = str(self.scenario_id)
        self.channel = AgentLeakChannel(self.channel)
        self.allowed_fields = {str(field) for field in self.allowed_fields}
        self.vault = dict(self.vault)
        self.metadata = dict(self.metadata)
        self.run_id = str(self.run_id)
        self.system = str(self.system)
        self.model = str(self.model)
        self.event_type = AgentLeakEventType(
            self.event_type or event_type_for_channel(self.channel)
        )
        self.source = self.source if self.source is not None else self.actor
        self.target = self.target if self.target is not None else self.recipient
        if self.actor is None:
            self.actor = self.source
        if self.recipient is None:
            self.recipient = self.target
        self.content_ref = self.content_ref or f"unmaterialized://{self.trace_id}/{self.channel.value}"
        self.flow_items = [_normalize_flow_item(item) for item in self.flow_items]
        self.agentleak_eval = _normalize_agentleak_eval(
            self.agentleak_eval,
            leaked=self.leaked,
            leakage_labels=self.leakage_labels,
        )

    def to_json(self) -> dict[str, Any]:
        payload = self.to_protocol_json()
        payload.update(
            {
                # Backwards-compatible Task 2/6A fixture fields. These are
                # conveniences, not a replacement for the protocol fields above.
                "actor": self.actor,
                "recipient": self.recipient,
                "content": self.content,
                "vault": self.vault,
                "allowed_fields": sorted(self.allowed_fields),
                "policy_id": self.policy_id,
                "leaked": self.leaked,
                "leakage_labels": self.leakage_labels,
                "metadata": self.metadata,
            }
        )
        return payload

    def to_protocol_json(self) -> dict[str, Any]:
        """Return the Phase 5 protocol-facing normalized trace row."""

        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "scenario_id": self.scenario_id,
            "system": self.system,
            "model": self.model,
            "channel": self.channel.value,
            "event_type": self.event_type.value,
            "source": self.source or "unknown",
            "target": self.target,
            "content_ref": self.content_ref,
            "flow_items": self.flow_items,
            "agentleak_eval": self.agentleak_eval,
            "contexthub_decision_ref": self.contexthub_decision_ref,
        }


@dataclass
class CompiledAgentLeakPolicy:
    """Offline policy oracle compiled from AgentLeak scenario annotations."""

    scenario_id: str
    uri_by_field: dict[str, str]
    allowed_fields: set[str]
    forbidden_fields: set[str]
    field_actions: dict[str, str]
    field_values: dict[str, Any]
    policy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.scenario_id = str(self.scenario_id)
        self.policy_id = self.policy_id or f"agentleak-policy:{self.scenario_id}"
        self.uri_by_field = {str(key): str(value) for key, value in self.uri_by_field.items()}
        self.allowed_fields = {str(field) for field in self.allowed_fields}
        self.forbidden_fields = {str(field) for field in self.forbidden_fields}
        self.field_actions = {
            str(field): str(action) for field, action in self.field_actions.items()
        }
        self.field_values = dict(self.field_values)
        self.metadata = dict(self.metadata)

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "policy_id": self.policy_id,
            "uri_by_field": self.uri_by_field,
            "allowed_fields": sorted(self.allowed_fields),
            "forbidden_fields": sorted(self.forbidden_fields),
            "field_actions": self.field_actions,
            "field_values": self.field_values,
            "metadata": self.metadata,
        }


def event_type_for_channel(channel: AgentLeakChannel | str) -> AgentLeakEventType:
    normalized = AgentLeakChannel(channel)
    return {
        AgentLeakChannel.C1: AgentLeakEventType.MESSAGE_OUT,
        AgentLeakChannel.C2: AgentLeakEventType.AGENT_MESSAGE,
        AgentLeakChannel.C3: AgentLeakEventType.TOOL_CALL,
        AgentLeakChannel.C4: AgentLeakEventType.TOOL_RESULT,
        AgentLeakChannel.C5: AgentLeakEventType.MEMORY_WRITE,
        AgentLeakChannel.C6: AgentLeakEventType.LOG_EVENT,
        AgentLeakChannel.C7: AgentLeakEventType.ARTIFACT_WRITE,
    }[normalized]


def _normalize_flow_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"uri": "", "field_names": []}
    uri = str(item.get("uri") or "")
    field_names = item.get("field_names")
    if not isinstance(field_names, list):
        fields = item.get("fields")
        field_names = sorted(str(key) for key in fields) if isinstance(fields, dict) else []
    return {"uri": uri, "field_names": [str(field) for field in field_names]}


def _normalize_agentleak_eval(
    payload: dict[str, Any],
    *,
    leaked: bool | None,
    leakage_labels: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = dict(payload)
    if "has_leak" not in normalized:
        normalized["has_leak"] = leaked
    if "leaked_fields" not in normalized:
        labels = leakage_labels or {}
        leaked_fields = labels.get("leaked_fields") if isinstance(labels, dict) else None
        normalized["leaked_fields"] = list(leaked_fields) if isinstance(leaked_fields, list) else []
    if "detector_mode" not in normalized:
        normalized["detector_mode"] = "not_run"
    return normalized
