from __future__ import annotations

from dataclasses import dataclass, field
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from integrations.agentleak.mapping import policy_to_flow_items
from integrations.agentleak.policy_compiler import compile_policy
from integrations.agentleak.trace_schema import (
    AgentLeakChannel,
    AgentLeakEventType,
    AgentLeakTraceEvent,
)


@dataclass
class AgentLeakLoadResult:
    events: list[AgentLeakTraceEvent] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def load_trace_jsonl(path: str | Path) -> list[AgentLeakTraceEvent]:
    """Load normalized events from a JSONL trace file."""

    return load_trace_jsonl_with_warnings(path).events


def load_trace_jsonl_with_warnings(path: str | Path) -> AgentLeakLoadResult:
    """Load JSONL and preserve fail-soft warnings for callers that need them."""

    result = AgentLeakLoadResult()
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                result.warnings.append(
                    {
                        "line": line_number,
                        "skipped_reason": "invalid_json",
                        "error": str(exc),
                    }
                )
                continue
            if not isinstance(record, Mapping):
                result.warnings.append(
                    {"line": line_number, "skipped_reason": "record_not_object"}
                )
                continue
            normalized = normalize_trace_record_with_warnings(record)
            result.events.extend(normalized.events)
            for warning in normalized.warnings:
                result.warnings.append({"line": line_number, **warning})
    return result


def normalize_trace_record(
    record: Mapping[str, Any],
    *,
    scenario: Mapping[str, Any] | None = None,
) -> list[AgentLeakTraceEvent]:
    """Normalize one fixture-compatible AgentLeak trace record."""

    return normalize_trace_record_with_warnings(record, scenario=scenario).events


def normalize_trace_record_with_warnings(
    record: Mapping[str, Any],
    *,
    scenario: Mapping[str, Any] | None = None,
) -> AgentLeakLoadResult:
    """Normalize one record without assuming the real AgentLeak schema is frozen."""

    scenario_record = _scenario(record, scenario)
    policy = compile_policy(scenario_record)
    trace_id = str(record.get("trace_id") or record.get("id") or policy.scenario_id)
    scenario_id = policy.scenario_id
    run_id = str(record.get("run_id") or "fixture-run")
    system = str(record.get("system") or "unknown-system")
    model = str(record.get("model") or "unknown-model")
    common = {
        "run_id": run_id,
        "trace_id": trace_id,
        "scenario_id": scenario_id,
        "system": system,
        "model": model,
        "vault": policy.field_values,
        "allowed_fields": policy.allowed_fields,
        "policy_id": policy.policy_id,
    }

    result = AgentLeakLoadResult()
    for event_index, raw_event in enumerate(_iter_raw_events(record)):
        channel = _channel(raw_event)
        if channel is None:
            result.warnings.append(
                {
                    "trace_id": trace_id,
                    "skipped_reason": "unknown_channel",
                    "raw": dict(raw_event),
                }
            )
            continue
        event = AgentLeakTraceEvent(
            **common,
            channel=channel,
            actor=_first(raw_event, "actor", "source", "sender", "source_agent", "agent"),
            recipient=_first(raw_event, "recipient", "target", "receiver", "target_agent", "to"),
            content=_content(raw_event),
            leaked=_optional_bool(raw_event.get("leaked")),
            leakage_labels=_optional_dict(raw_event.get("leakage_labels")),
            event_type=_event_type(raw_event),
            source=_first(raw_event, "source", "actor", "sender", "source_agent", "agent"),
            target=_first(raw_event, "target", "recipient", "receiver", "target_agent", "to"),
            content_ref=str(
                raw_event.get("content_ref")
                or f"fixture://{run_id}/{trace_id}/{channel.value}/{event_index}"
            ),
            flow_items=_flow_items(raw_event, policy),
            agentleak_eval=_agentleak_eval(raw_event),
            contexthub_decision_ref=_optional_str(raw_event.get("contexthub_decision_ref")),
            metadata={
                "raw": dict(raw_event),
                "format_status": "fixture-compatible-unfrozen",
                "uses_online_llm_or_detector": False,
            },
        )
        result.events.append(event)

    observed = {event.channel for event in result.events}
    for expected in (AgentLeakChannel.C2, AgentLeakChannel.C5):
        if expected not in observed:
            result.warnings.append(
                {
                    "trace_id": trace_id,
                    "channel": expected.value,
                    "skipped_reason": "channel_missing",
                }
            )
    return result


def _scenario(
    record: Mapping[str, Any],
    scenario: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if scenario is not None:
        return scenario
    embedded = record.get("scenario")
    if isinstance(embedded, Mapping):
        return embedded
    return record


def _iter_raw_events(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    # ``channel_messages`` is the key emitted by the real AgentLeak
    # ``benchmarks/ieee_repro/benchmark.py`` runner; each item already carries a
    # ``channel`` label (e.g. "C1"/"C2"/"C5"). The other keys are fixture-format
    # aliases. All are treated as pre-labeled event lists.
    for key in ("events", "trace", "channel_events", "channel_messages"):
        value = record.get(key)
        if isinstance(value, list):
            events.extend(item for item in value if isinstance(item, Mapping))

    for key in ("inter_agent_messages", "handoffs", "messages"):
        value = record.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    events.append({"channel": "C2", **item})

    for key in ("shared_memory", "memory_events", "memory"):
        value = record.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    events.append({"channel": "C5", **item})

    if "final_output" in record:
        events.append({"channel": "C1", "content": record.get("final_output")})
    elif "output" in record:
        events.append({"channel": "C1", "content": record.get("output")})
    return events


def _channel(raw_event: Mapping[str, Any]) -> AgentLeakChannel | None:
    raw = str(
        raw_event.get("channel")
        or raw_event.get("channel_id")
        or raw_event.get("type")
        or raw_event.get("event_type")
        or ""
    ).strip()
    normalized = raw.upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "C1": AgentLeakChannel.C1,
        "C1_FINAL_OUTPUT": AgentLeakChannel.C1,
        "FINAL_OUTPUT": AgentLeakChannel.C1,
        "OUTPUT": AgentLeakChannel.C1,
        "C2": AgentLeakChannel.C2,
        "C2_INTER_AGENT": AgentLeakChannel.C2,
        "INTER_AGENT_MESSAGE": AgentLeakChannel.C2,
        "INTER_AGENT_MESSAGES": AgentLeakChannel.C2,
        "INTER_AGENT": AgentLeakChannel.C2,
        "HANDOFF": AgentLeakChannel.C2,
        "MESSAGE": AgentLeakChannel.C2,
        "C3": AgentLeakChannel.C3,
        "C3_TOOL_INPUT": AgentLeakChannel.C3,
        "TOOL_INPUT": AgentLeakChannel.C3,
        "TOOL_CALL": AgentLeakChannel.C3,
        "C4": AgentLeakChannel.C4,
        "C4_TOOL_OUTPUT": AgentLeakChannel.C4,
        "TOOL_OUTPUT": AgentLeakChannel.C4,
        "TOOL_RESULT": AgentLeakChannel.C4,
        "C5": AgentLeakChannel.C5,
        "C5_MEMORY_WRITE": AgentLeakChannel.C5,
        "SHARED_MEMORY": AgentLeakChannel.C5,
        "MEMORY": AgentLeakChannel.C5,
        "MEMORY_WRITE": AgentLeakChannel.C5,
        "C6": AgentLeakChannel.C6,
        "C6_LOG": AgentLeakChannel.C6,
        "LOG": AgentLeakChannel.C6,
        "LOG_EVENT": AgentLeakChannel.C6,
        "C7": AgentLeakChannel.C7,
        "C7_ARTIFACT": AgentLeakChannel.C7,
        "ARTIFACT": AgentLeakChannel.C7,
        "ARTIFACT_WRITE": AgentLeakChannel.C7,
    }
    return aliases.get(normalized)


def _first(raw_event: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw_event.get(key)
        if value is not None:
            return str(value)
    return None


def _content(raw_event: Mapping[str, Any]) -> str | dict[str, Any]:
    for key in ("content", "message", "text", "value", "final_output"):
        value = raw_event.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if value is not None:
            return str(value)
    return ""


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _event_type(raw_event: Mapping[str, Any]) -> str | None:
    value = raw_event.get("event_type")
    if value is None:
        return None
    candidate = str(value)
    return candidate if candidate in {event_type.value for event_type in AgentLeakEventType} else None


def _flow_items(
    raw_event: Mapping[str, Any],
    policy,
) -> list[dict[str, Any]]:
    flow_items = raw_event.get("flow_items")
    if isinstance(flow_items, list):
        return [dict(item) for item in flow_items if isinstance(item, Mapping)]
    return policy_to_flow_items(policy)


def _agentleak_eval(raw_event: Mapping[str, Any]) -> dict[str, Any]:
    payload = raw_event.get("agentleak_eval")
    if isinstance(payload, Mapping):
        return dict(payload)
    labels = _optional_dict(raw_event.get("leakage_labels")) or {}
    # Real AgentLeak benchmark events carry post-hoc detector labels directly as
    # ``has_leak`` / ``leaked_fields``; fixtures use ``leaked`` / ``leakage_labels``.
    # These are evaluation fields only; the loader never runs a detector here.
    leaked = raw_event.get("leaked")
    if leaked is None:
        leaked = raw_event.get("has_leak")
    leaked_fields = labels.get("leaked_fields")
    if not isinstance(leaked_fields, list):
        leaked_fields = raw_event.get("leaked_fields")
    return {
        "has_leak": _optional_bool(leaked),
        "leaked_fields": list(leaked_fields) if isinstance(leaked_fields, list) else [],
        "detector_mode": str(
            raw_event.get("detector_mode")
            or labels.get("detector_mode")
            or "not_run"
        ),
    }


__all__ = [
    "AgentLeakLoadResult",
    "load_trace_jsonl",
    "load_trace_jsonl_with_warnings",
    "normalize_trace_record",
    "normalize_trace_record_with_warnings",
]
