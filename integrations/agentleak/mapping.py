from __future__ import annotations

from typing import Any
from urllib.parse import quote

from contexthub.enforcement.context import Boundary
from integrations.agentleak.trace_schema import (
    AgentLeakChannel,
    AgentLeakTraceEvent,
    CompiledAgentLeakPolicy,
)


def field_uri(scenario_id: str, record_index: int | str, field_name: str) -> str:
    """Return the stable ContextHub URI for one AgentLeak vault field."""

    scenario = quote(str(scenario_id), safe="")
    record = quote(str(record_index), safe="")
    field = quote(str(field_name), safe="")
    return f"ctx://agentleak/{scenario}/{record}/{field}"


def channel_to_boundary(channel: AgentLeakChannel | str) -> Boundary | None:
    """Map AgentLeak channel to a ContextHub boundary.

    C1 is final output and remains supplemental/audit-only for Task 2, so it does
    not map to an enforcement boundary.
    """

    normalized = AgentLeakChannel(channel)
    if normalized == AgentLeakChannel.C2:
        return Boundary.HANDOFF
    if normalized == AgentLeakChannel.C5:
        return Boundary.SHARED_MEMORY_WRITE
    if normalized == AgentLeakChannel.C3:
        return Boundary.TOOL_CALL
    return None


def policy_to_flow_payload(
    policy: CompiledAgentLeakPolicy,
    *,
    fields: set[str] | None = None,
    include_values: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Convert compiled policy fields into FlowGuardrail-compatible payload."""

    selected = set(fields) if fields is not None else set(policy.field_values)
    items: list[dict[str, Any]] = []
    for field in sorted(selected):
        if field not in policy.uri_by_field:
            continue
        value = policy.field_values.get(field)
        payload_fields: dict[str, Any] = {}
        if include_values:
            payload_fields[field] = value
        else:
            payload_fields[field] = {"present": field in policy.field_values}
        items.append({"uri": policy.uri_by_field[field], "fields": payload_fields})
    return {"items": items}


def policy_to_flow_items(
    policy: CompiledAgentLeakPolicy,
    *,
    fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return protocol normalized-trace flow item references without values."""

    selected = set(fields) if fields is not None else set(policy.field_values)
    items: list[dict[str, Any]] = []
    for field in sorted(selected):
        if field not in policy.uri_by_field:
            continue
        items.append({"uri": policy.uri_by_field[field], "field_names": [field]})
    return items


def event_to_flow_payload(
    event: AgentLeakTraceEvent,
    policy: CompiledAgentLeakPolicy,
    *,
    include_values: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Build a best-effort structured flow payload for a normalized event.

    This only projects known scenario vault fields. Free-text content remains in
    ``event.content`` and is not semantically interpreted here.
    """

    return policy_to_flow_payload(policy, fields=set(event.vault), include_values=include_values)


__all__ = [
    "channel_to_boundary",
    "event_to_flow_payload",
    "field_uri",
    "policy_to_flow_items",
    "policy_to_flow_payload",
]
