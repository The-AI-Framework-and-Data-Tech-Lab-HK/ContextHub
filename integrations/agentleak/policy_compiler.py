from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from integrations.agentleak.mapping import field_uri
from integrations.agentleak.trace_schema import CompiledAgentLeakPolicy

_POLICY_ACTIONS = frozenset({"allow", "mask", "deny"})


def compile_policy(
    scenario: Mapping[str, Any],
    *,
    forbidden_action: str = "deny",
) -> CompiledAgentLeakPolicy:
    """Compile AgentLeak scenario annotations into an offline ContextHub policy.

    The compiler only consumes structured ``private_vault`` / ``allowed_set``
    data. It does not call AgentLeak detectors, LLM judges, or any runtime policy
    oracle.
    """

    if forbidden_action not in _POLICY_ACTIONS - {"allow"}:
        raise ValueError("forbidden_action must be 'deny' or 'mask'")

    scenario_id = _scenario_id(scenario)
    flattened = _flatten_private_vault(scenario.get("private_vault"))
    allowed_raw = _field_set(_allowed_set(scenario).get("fields"))
    forbidden_raw = _field_set(_allowed_set(scenario).get("forbidden_fields"))

    uri_by_field: dict[str, str] = {}
    field_values: dict[str, Any] = {}
    raw_name_by_field: dict[str, str] = {}
    for field_key, raw_field, record_id, value in flattened:
        uri_by_field[field_key] = field_uri(scenario_id, record_id, raw_field)
        field_values[field_key] = value
        raw_name_by_field[field_key] = raw_field

    allowed_fields = {
        field_key
        for field_key, raw_field in raw_name_by_field.items()
        if field_key in allowed_raw or raw_field in allowed_raw
    }
    explicit_forbidden = {
        field_key
        for field_key, raw_field in raw_name_by_field.items()
        if field_key in forbidden_raw or raw_field in forbidden_raw
    }
    implicit_forbidden = set(field_values) - allowed_fields - explicit_forbidden
    forbidden_fields = explicit_forbidden | implicit_forbidden

    field_actions: dict[str, str] = {}
    for field in field_values:
        if field in allowed_fields:
            field_actions[field] = "allow"
        elif field in forbidden_fields:
            field_actions[field] = forbidden_action

    metadata = {
        "source": "agentleak_offline_policy_compiler",
        "uses_online_llm_or_detector": False,
        "forbidden_action": forbidden_action,
        "raw_allowed_fields": sorted(allowed_raw),
        "raw_forbidden_fields": sorted(forbidden_raw),
        "raw_name_by_field": raw_name_by_field,
        "implicit_forbidden": bool(implicit_forbidden),
        "implicit_forbidden_fields": sorted(implicit_forbidden),
        "format_status": "fixture-compatible-unfrozen",
    }

    return CompiledAgentLeakPolicy(
        scenario_id=scenario_id,
        policy_id=f"agentleak-policy:{scenario_id}",
        uri_by_field=uri_by_field,
        allowed_fields=allowed_fields,
        forbidden_fields=forbidden_fields,
        field_actions=field_actions,
        field_values=field_values,
        metadata=metadata,
    )


def _scenario_id(scenario: Mapping[str, Any]) -> str:
    value = (
        scenario.get("scenario_id")
        or scenario.get("id")
        or scenario.get("name")
        or scenario.get("task_id")
    )
    if value is None:
        return "unknown-scenario"
    return str(value)


def _allowed_set(scenario: Mapping[str, Any]) -> Mapping[str, Any]:
    allowed = scenario.get("allowed_set")
    return allowed if isinstance(allowed, Mapping) else {}


def _field_set(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key) for key, enabled in value.items() if bool(enabled)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value}
    if isinstance(value, str):
        return {value}
    return set()


def _flatten_private_vault(vault: Any) -> list[tuple[str, str, str, Any]]:
    """Return ``(field_key, raw_field, record_id, value)`` tuples."""

    records = _vault_records(vault)
    raw_counts: dict[str, int] = {}
    raw_entries: list[tuple[str, str, Any]] = []
    for record_index, record in enumerate(records):
        fields = record.get("fields") if isinstance(record, Mapping) else None
        if not isinstance(fields, Mapping):
            continue
        record_id = _record_id(record, record_index)
        for raw_field, value in fields.items():
            field_name = str(raw_field)
            raw_counts[field_name] = raw_counts.get(field_name, 0) + 1
            raw_entries.append((field_name, record_id, value))

    flattened: list[tuple[str, str, str, Any]] = []
    for raw_field, record_id, value in raw_entries:
        field_key = raw_field if raw_counts[raw_field] == 1 else f"{record_id}.{raw_field}"
        flattened.append((field_key, raw_field, record_id, value))
    return flattened


def _record_id(record: Mapping[str, Any], record_index: int) -> str:
    value = record.get("record_id") or record.get("id")
    if value is not None:
        return str(value)
    return f"record_{record_index:03d}"


def _vault_records(vault: Any) -> list[Mapping[str, Any]]:
    if isinstance(vault, Mapping) and isinstance(vault.get("records"), list):
        return [record for record in vault["records"] if isinstance(record, Mapping)]
    if isinstance(vault, Mapping) and isinstance(vault.get("fields"), Mapping):
        return [vault]
    if isinstance(vault, Mapping):
        return [{"fields": vault}]
    return []


__all__ = ["compile_policy"]
