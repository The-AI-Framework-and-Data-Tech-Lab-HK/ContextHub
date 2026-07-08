from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from integrations.agentleak.mapping import policy_to_flow_items, policy_to_flow_payload
from integrations.agentleak.policy_compiler import compile_policy
from integrations.agentleak.trace_schema import (
    AgentLeakChannel,
    AgentLeakEventType,
    AgentLeakTraceEvent,
    CompiledAgentLeakPolicy,
)


@dataclass
class AgentLeakSecondaryLoadResult:
    events: list[AgentLeakTraceEvent] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class C7ReproducibilityFinding:
    status: str
    main_table_eligible: bool
    appendix_eligible: bool
    reason: str
    evidence: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "channel": AgentLeakChannel.C7.value,
            "status": self.status,
            "main_table_eligible": self.main_table_eligible,
            "appendix_eligible": self.appendix_eligible,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


def load_secondary_trace_json(path: str | Path) -> list[AgentLeakTraceEvent]:
    """Load C3/C6 normalized events from one AgentLeak tools trace JSON file."""

    return load_secondary_trace_json_with_warnings(path).events


def load_secondary_trace_json_with_warnings(
    path: str | Path,
) -> AgentLeakSecondaryLoadResult:
    result = AgentLeakSecondaryLoadResult()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.warnings.append({"skipped_reason": "invalid_json", "error": str(exc)})
        return result
    if not isinstance(payload, Mapping):
        result.warnings.append({"skipped_reason": "record_not_object"})
        return result
    return normalize_secondary_trace_record_with_warnings(payload)


def normalize_secondary_trace_record(
    record: Mapping[str, Any],
    *,
    scenario: Mapping[str, Any] | None = None,
) -> list[AgentLeakTraceEvent]:
    """Normalize AgentLeak `benchmark_tools.py` traces for C3 and C6."""

    return normalize_secondary_trace_record_with_warnings(
        record,
        scenario=scenario,
    ).events


def normalize_secondary_trace_record_with_warnings(
    record: Mapping[str, Any],
    *,
    scenario: Mapping[str, Any] | None = None,
) -> AgentLeakSecondaryLoadResult:
    scenario_record = _scenario_from_tools_trace(record, scenario)
    policy = compile_policy(scenario_record)
    trace_id = str(record.get("trace_id") or record.get("id") or policy.scenario_id)
    run_id = str(record.get("run_id") or "fixture-run")
    model = str(record.get("model") or "unknown-model")
    system = str(record.get("system") or "unknown-system")

    result = AgentLeakSecondaryLoadResult()
    for event_index, raw_event in enumerate(_iter_secondary_channel_messages(record)):
        channel = _channel(raw_event)
        if channel not in {AgentLeakChannel.C3, AgentLeakChannel.C6}:
            result.warnings.append(
                {
                    "trace_id": trace_id,
                    "skipped_reason": "unsupported_secondary_channel",
                    "channel": str(raw_event.get("channel") or ""),
                }
            )
            continue

        content = _content(raw_event)
        fields = _matched_policy_fields(content, policy)
        metadata = _secondary_metadata(channel, raw_event, content, policy, fields)
        if not fields:
            metadata["flow_mapping_status"] = "no_structured_or_exact_match"
            metadata["diagnostic_reason"] = (
                "semantic_or_paraphrase_leakage_requires_post_hoc_agentleak_detector"
            )

        event = AgentLeakTraceEvent(
            run_id=run_id,
            trace_id=trace_id,
            scenario_id=policy.scenario_id,
            system=system,
            model=model,
            channel=channel,
            actor=_first(raw_event, "actor", "source", "agent", "component"),
            recipient=_first(raw_event, "recipient", "target", "tool", "sink"),
            content=content,
            vault=policy.field_values,
            allowed_fields=policy.allowed_fields,
            policy_id=policy.policy_id,
            leaked=_optional_bool(_first_present(raw_event, "has_leak", "leaked")),
            leakage_labels={"leaked_fields": _list_str(raw_event.get("leaked_fields"))},
            event_type=AgentLeakEventType.TOOL_CALL
            if channel == AgentLeakChannel.C3
            else AgentLeakEventType.LOG_EVENT,
            source=_first(raw_event, "source", "actor", "agent", "component"),
            target=_first(raw_event, "target", "recipient", "tool", "sink"),
            content_ref=str(
                raw_event.get("content_ref")
                or f"fixture://{run_id}/{trace_id}/{channel.value}/{event_index}"
            ),
            flow_items=policy_to_flow_items(policy, fields=fields),
            agentleak_eval={
                "has_leak": _optional_bool(_first_present(raw_event, "has_leak", "leaked")),
                "leaked_fields": _list_str(raw_event.get("leaked_fields")),
                "detector_mode": str(raw_event.get("detector_mode") or "script_exact"),
            },
            contexthub_decision_ref=_optional_str(raw_event.get("contexthub_decision_ref")),
            metadata=metadata,
        )
        result.events.append(event)

    observed = {event.channel for event in result.events}
    for expected in (AgentLeakChannel.C3, AgentLeakChannel.C6):
        if expected not in observed:
            result.warnings.append(
                {
                    "trace_id": trace_id,
                    "channel": expected.value,
                    "skipped_reason": "channel_missing",
                }
            )
    return result


def secondary_event_to_flow_payload(
    event: AgentLeakTraceEvent,
    policy: CompiledAgentLeakPolicy,
    *,
    include_values: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Build a FlowGuardrail payload from fields observed in C3/C6 content."""

    fields = _matched_policy_fields(event.content, policy)
    return policy_to_flow_payload(policy, fields=fields, include_values=include_values)


def assess_c7_reproducibility(
    agentleak_root: str | Path | None = None,
) -> C7ReproducibilityFinding:
    """Assess whether C7 has a public runner aligned with IEEE reproduction traces."""

    root = Path(agentleak_root) if agentleak_root is not None else None
    evidence = [
        "benchmarks/ieee_repro/benchmark.py covers C1/C2/C5",
        "benchmarks/ieee_repro/benchmark_tools.py covers C3/C6",
        "benchmarks/showcase contains SDK demos and traces, not a scenario-subset runner",
    ]
    if root is not None:
        artifact_runners = [
            root / "benchmarks/ieee_repro/benchmark_artifacts.py",
            root / "benchmarks/ieee_repro/benchmark_c7.py",
        ]
        if any(path.exists() for path in artifact_runners):
            return C7ReproducibilityFinding(
                status="available",
                main_table_eligible=True,
                appendix_eligible=True,
                reason="public_ieee_repro_artifact_runner_found",
                evidence=[str(path) for path in artifact_runners if path.exists()],
            )

    return C7ReproducibilityFinding(
        status="skipped",
        main_table_eligible=False,
        appendix_eligible=True,
        reason=(
            "no_public_ieee_repro_runner_for_c7_artifacts; keep C7 as appendix or "
            "future work unless a manifest-grade runner is added before formal runs"
        ),
        evidence=evidence,
    )


def _scenario_from_tools_trace(
    record: Mapping[str, Any],
    scenario: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if scenario is not None:
        return scenario
    embedded = record.get("scenario")
    if isinstance(embedded, Mapping):
        return embedded
    input_payload = record.get("input")
    if isinstance(input_payload, Mapping):
        return {
            "scenario_id": record.get("scenario_id") or record.get("trace_id"),
            "private_vault": input_payload.get("vault") or {},
            "allowed_set": input_payload.get("allowed_set") or {},
        }
    return record


def _iter_secondary_channel_messages(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    for key in ("channel_messages", "events", "trace", "channel_events"):
        value = record.get(key)
        if isinstance(value, list):
            events.extend(item for item in value if isinstance(item, Mapping))

    for key in ("tool_calls", "tool_inputs"):
        value = record.get(key)
        if isinstance(value, list):
            events.extend({"channel": "C3", **item} for item in value if isinstance(item, Mapping))

    for key in ("logs", "log_events"):
        value = record.get(key)
        if isinstance(value, list):
            events.extend({"channel": "C6", **item} for item in value if isinstance(item, Mapping))
    return events


def _channel(raw_event: Mapping[str, Any]) -> AgentLeakChannel | None:
    raw = str(raw_event.get("channel") or raw_event.get("type") or "").strip()
    normalized = raw.upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "C3": AgentLeakChannel.C3,
        "C3_TOOL_INPUT": AgentLeakChannel.C3,
        "TOOL_INPUT": AgentLeakChannel.C3,
        "TOOL_CALL": AgentLeakChannel.C3,
        "C6": AgentLeakChannel.C6,
        "C6_LOG": AgentLeakChannel.C6,
        "LOG": AgentLeakChannel.C6,
        "LOG_EVENT": AgentLeakChannel.C6,
    }
    return aliases.get(normalized)


def _content(raw_event: Mapping[str, Any]) -> str | dict[str, Any]:
    for key in ("content", "message", "text", "payload", "arguments", "args", "params"):
        value = raw_event.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, list):
            return {"items": value}
        if value is not None:
            return str(value)
    return ""


def _secondary_metadata(
    channel: AgentLeakChannel,
    raw_event: Mapping[str, Any],
    content: str | dict[str, Any],
    policy: CompiledAgentLeakPolicy,
    fields: set[str],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "raw": dict(raw_event),
        "format_status": "agentleak_benchmark_tools_compatible",
        "uses_online_llm_or_detector": False,
        "sensitive_fields_in_content": sorted(fields),
    }
    if channel == AgentLeakChannel.C3:
        tool_payload = _parse_json_object(content)
        metadata.update(
            {
                "logical_boundary": "tool_call",
                "tool_name": _tool_name(raw_event, tool_payload),
                "tool_arguments": _tool_arguments(tool_payload),
                "sensitive_fields_in_arguments": sorted(fields),
            }
        )
    else:
        structured_fields = _structured_log_fields(raw_event, content)
        structured_matches = _matched_policy_fields(structured_fields, policy)
        metadata.update(
            {
                "logical_boundary": "log_persistence",
                "log_source": _first(raw_event, "source", "component", "logger") or "unknown",
                "log_level": _log_level(raw_event, content),
                "structured_fields": structured_fields,
                "structured_sensitive_fields": sorted(structured_matches),
            }
        )
    return metadata


def _matched_policy_fields(
    content: Any,
    policy: CompiledAgentLeakPolicy,
) -> set[str]:
    raw_name_by_field = policy.metadata.get("raw_name_by_field")
    raw_name_by_field = raw_name_by_field if isinstance(raw_name_by_field, Mapping) else {}
    text_fragments: list[str] = []
    keys: set[str] = set()
    for key, value in _walk(content):
        if key is not None:
            keys.add(str(key))
        if value is not None:
            text_fragments.append(str(value))
    joined = "\n".join(text_fragments).lower()

    matched: set[str] = set()
    for field, value in policy.field_values.items():
        raw_name = str(raw_name_by_field.get(field) or field)
        if field in keys or raw_name in keys:
            matched.add(field)
            continue
        value_text = str(value)
        if len(value_text) > 3 and value_text.lower() in joined:
            matched.add(field)
    return matched


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)
    else:
        yield None, value


def _parse_json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        return {}
    text = content.strip()
    if text.startswith("JSON:"):
        text = text[5:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _tool_name(raw_event: Mapping[str, Any], tool_payload: Mapping[str, Any]) -> str | None:
    for source in (raw_event, tool_payload):
        for key in ("tool_name", "tool", "name"):
            value = source.get(key)
            if value is not None:
                return str(value)
    return None


def _tool_arguments(tool_payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("params", "arguments", "args", "input"):
        value = tool_payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return dict(tool_payload)


def _structured_log_fields(
    raw_event: Mapping[str, Any],
    content: str | dict[str, Any],
) -> dict[str, Any]:
    explicit = raw_event.get("structured_fields")
    if isinstance(explicit, Mapping):
        return dict(explicit)
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        return {}
    fields: dict[str, Any] = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key and value and len(key) < 64:
            fields[key] = value
    return fields


def _log_level(raw_event: Mapping[str, Any], content: str | dict[str, Any]) -> str | None:
    level = raw_event.get("level") or raw_event.get("log_level")
    if level is not None:
        return str(level)
    if isinstance(content, str):
        match = re.match(r"\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", content)
        if match:
            return match.group(1).lower()
    return None


def _first(raw_event: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw_event.get(key)
        if value is not None:
            return str(value)
    return None


def _first_present(raw_event: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw_event:
            return raw_event[key]
    return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _list_str(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


__all__ = [
    "AgentLeakSecondaryLoadResult",
    "C7ReproducibilityFinding",
    "assess_c7_reproducibility",
    "load_secondary_trace_json",
    "load_secondary_trace_json_with_warnings",
    "normalize_secondary_trace_record",
    "normalize_secondary_trace_record_with_warnings",
    "secondary_event_to_flow_payload",
]
