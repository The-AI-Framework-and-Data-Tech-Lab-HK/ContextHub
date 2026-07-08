from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from contexthub.enforcement.context import Boundary
from contexthub.enforcement.decision import GuardrailDecision
from integrations.agentleak.trace_schema import AgentLeakTraceEvent


def build_decision_log_record(
    *,
    event: AgentLeakTraceEvent,
    decision: GuardrailDecision,
    boundary: Boundary,
    protocol_boundary: str | None = None,
    system: str = "AL-S3",
) -> dict[str, Any]:
    """Build a scrubbed Phase 5 decision log row.

    The record intentionally stores only URI and field-name summaries. It does
    not persist raw event content, vault values, or sanitized field values.
    """

    sanitized_payload = decision.sanitized_payload if isinstance(decision.sanitized_payload, Mapping) else {}
    original_uris = _flow_item_uris(event.flow_items)
    kept_uris = _payload_uris(sanitized_payload)
    masked_fields = _masked_fields(decision)
    dropped_uris = sorted(set(original_uris) - set(kept_uris))

    row = {
        "decision_id": _decision_id(event, decision, original_uris, masked_fields, dropped_uris),
        "run_id": event.run_id,
        "trace_id": event.trace_id,
        "scenario_id": event.scenario_id,
        "channel": event.channel.value,
        "boundary": protocol_boundary or boundary.value,
        "actor": event.actor or event.source or "unknown",
        "recipient": event.recipient or event.target,
        "system": system,
        "verdict": decision.verdict.value,
        "guardrail": decision.guardrail or "flow",
        "violation_kinds": sorted({violation.kind.value for violation in decision.violations}),
        "flow_item_uris": original_uris,
        "flow_item_field_names": _flow_item_field_names(event.flow_items),
        "masked_fields": masked_fields,
        "dropped_uris": dropped_uris,
        "sanitized_payload_ref": _payload_ref(sanitized_payload) if sanitized_payload else None,
        "semantic_unmapped": bool(event.metadata.get("semantic_unmapped")),
    }
    return row


def summarize_flow_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a value-free summary suitable for logs and manifests."""

    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return {"item_count": 0, "uris": [], "field_names": []}
    return {
        "item_count": len(items),
        "uris": _payload_uris(payload),
        "field_names": _payload_field_names(payload),
    }


def _decision_id(
    event: AgentLeakTraceEvent,
    decision: GuardrailDecision,
    uris: list[str],
    masked_fields: list[str],
    dropped_uris: list[str],
) -> str:
    digest = hashlib.sha256()
    digest.update(event.run_id.encode("utf-8"))
    digest.update(event.trace_id.encode("utf-8"))
    digest.update(event.channel.value.encode("utf-8"))
    digest.update(decision.verdict.value.encode("utf-8"))
    digest.update(json.dumps(uris, sort_keys=True).encode("utf-8"))
    digest.update(json.dumps(masked_fields, sort_keys=True).encode("utf-8"))
    digest.update(json.dumps(dropped_uris, sort_keys=True).encode("utf-8"))
    return f"agentleak-decision:{digest.hexdigest()[:16]}"


def _payload_ref(payload: Mapping[str, Any]) -> str:
    summary = summarize_flow_payload(payload)
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _flow_item_uris(flow_items: list[dict[str, Any]]) -> list[str]:
    return sorted({str(item.get("uri")) for item in flow_items if item.get("uri")})


def _flow_item_field_names(flow_items: list[dict[str, Any]]) -> list[str]:
    fields: set[str] = set()
    for item in flow_items:
        names = item.get("field_names")
        if isinstance(names, list):
            fields.update(str(name) for name in names)
    return sorted(fields)


def _payload_uris(payload: Mapping[str, Any]) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return sorted({str(item.get("uri")) for item in items if isinstance(item, Mapping) and item.get("uri")})


def _payload_field_names(payload: Mapping[str, Any]) -> list[str]:
    items = payload.get("items")
    fields: set[str] = set()
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_fields = item.get("fields")
        if isinstance(item_fields, Mapping):
            fields.update(str(field) for field in item_fields)
    return sorted(fields)


def _masked_fields(decision: GuardrailDecision) -> list[str]:
    fields: set[str] = set()
    for violation in decision.violations:
        masked = violation.evidence.get("masked")
        if isinstance(masked, list):
            fields.update(str(field) for field in masked)
    return sorted(fields)


__all__ = ["build_decision_log_record", "summarize_flow_payload"]
