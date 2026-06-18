"""Trace-only action and argument alignment for EntCollabBench closure checks."""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from typing import Any

from integrations.entcollabbench.tool_contract_adapter import normalize_tool_args


FAILED_STATUSES = {"error", "failed", "failure", "timeout", "cancelled"}
IDENTITY_MARKERS = {
    "case_id",
    "knowledge_id",
    "incident_id",
    "change_id",
}
COLLABORATION_ALIAS_SERVICES = {
    "calendar",
    "email",
    "gmail",
    "outlook",
    "teams",
}

ActualToolCall = Callable[[dict[str, Any]], dict[str, Any] | None]


def align_ground_truth_to_trace(
    gt_steps: list[dict[str, Any]],
    trace_events: list[dict[str, Any]],
    *,
    actual_tool_call: ActualToolCall,
) -> dict[str, Any]:
    """Pair expected ground-truth actions with successful trace tool calls.

    Alignment is intentionally conservative: only identity/object-like argument
    mismatches are promoted to ``misaligned_actions``. Other argument
    differences remain diagnostic context.
    """

    expected_steps = [
        step
        for step in gt_steps
        if step.get("agent") and step.get("tool_name") and step.get("mcp_server_name")
    ]
    successful_calls = _successful_tool_calls(trace_events, actual_tool_call=actual_tool_call)
    calls_by_action: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for call in successful_calls:
        calls_by_action[_call_action_label(call)].append(call)

    missing_actions: list[str] = []
    misaligned_actions: list[str] = []
    argument_diffs: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}

    for index, expected in enumerate(expected_steps):
        action = _step_action_label(expected)
        queue = calls_by_action.get(action)
        if not queue:
            missing_actions.append(action)
            continue

        actual = queue.popleft()
        comparison = compare_expected_to_actual_args(expected, actual)
        evidence.update(_identity_evidence(action, comparison, actual))

        if comparison["identity_mismatches"] or comparison["non_identity_diffs"]:
            argument_diffs.append(
                {
                    "action": action,
                    "expected_index": index,
                    "expected_server": expected.get("mcp_server_name"),
                    "actual_server": actual.get("server"),
                    "evidence": actual.get("evidence"),
                    "identity_mismatches": comparison["identity_mismatches"],
                    "non_identity_diffs": comparison["non_identity_diffs"],
                    "soft_identity_diffs": comparison["soft_identity_diffs"],
                }
            )
        if comparison["identity_mismatches"]:
            misaligned_actions.append(action)

    return {
        "missing_actions": _unique(missing_actions),
        "misaligned_actions": _unique(misaligned_actions),
        "argument_diffs": argument_diffs,
        "evidence": evidence,
        "successful_call_count": len(successful_calls),
    }


def compare_expected_to_actual_args(
    expected_step: Mapping[str, Any],
    actual_call: Mapping[str, Any],
) -> dict[str, Any]:
    """Return identity-blocking and diagnostic-only argument diffs."""

    expected_args = normalize_tool_args(
        str(expected_step.get("mcp_server_name") or ""),
        str(expected_step.get("tool_name") or ""),
        _mapping_or_empty(expected_step.get("arguments")),
    )
    actual_args = normalize_tool_args(
        str(actual_call.get("server") or expected_step.get("mcp_server_name") or ""),
        str(actual_call.get("tool_name") or expected_step.get("tool_name") or ""),
        _mapping_or_empty(actual_call.get("tool_args")),
    )

    expected_identity = _identity_fields(expected_args)
    actual_identity = _identity_fields(actual_args)
    identity_mismatches, soft_identity_diffs = _identity_mismatches(
        expected_identity,
        actual_identity,
        expected_server=str(expected_step.get("mcp_server_name") or ""),
        actual_server=str(
            actual_call.get("server") or expected_step.get("mcp_server_name") or ""
        ),
    )
    non_identity_diffs = _non_identity_diffs(expected_args, actual_args)
    non_identity_diffs.extend(soft_identity_diffs)

    return {
        "expected_identity": expected_identity,
        "actual_identity": actual_identity,
        "identity_mismatches": identity_mismatches,
        "non_identity_diffs": non_identity_diffs,
        "soft_identity_diffs": soft_identity_diffs,
    }


def _successful_tool_calls(
    trace_events: list[dict[str, Any]],
    *,
    actual_tool_call: ActualToolCall,
) -> list[dict[str, Any]]:
    pending_by_id: dict[str, dict[str, Any]] = {}
    pending_by_agent_tool: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    successful: list[dict[str, Any]] = []

    for event in sorted(trace_events, key=lambda item: float(item.get("ts") or 0.0)):
        event_type = event.get("event")
        if event_type == "tool_call":
            call = actual_tool_call(event)
            if call is None:
                continue
            if call.get("tool_call_id"):
                pending_by_id[str(call["tool_call_id"])] = call
            pending_by_agent_tool[(str(call.get("agent")), str(call.get("tool_name")))].append(call)
            continue

        if event_type != "tool_result":
            continue

        call = _match_tool_result(event, pending_by_id, pending_by_agent_tool)
        if call is None:
            continue
        if _is_failed_tool_result(event):
            continue

        enriched = dict(call)
        enriched["result_event"] = event
        enriched["evidence"] = _evidence_ref(event, call)
        successful.append(enriched)

    return successful


def _identity_fields(value: Any) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for path, raw_value in _flatten_scalars(value):
        if not path or not _is_identity_key(path[-1]):
            continue
        canonical = _canonical_path(path)
        fields[canonical] = {
            "field": ".".join(path),
            "value": raw_value,
        }
    return fields


def _identity_mismatches(
    expected: dict[str, dict[str, Any]],
    actual: dict[str, dict[str, Any]],
    *,
    expected_server: str,
    actual_server: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mismatches: list[dict[str, Any]] = []
    soft_diffs: list[dict[str, Any]] = []
    generic_actual = actual.get("id")

    for canonical, expected_item in expected.items():
        actual_item = actual.get(canonical)
        if actual_item is None and canonical != "id":
            actual_item = generic_actual
        if actual_item is None:
            mismatches.append(
                {
                    "field": expected_item["field"],
                    "expected": expected_item["value"],
                    "actual": "<missing>",
                    "kind": "identity_missing",
                }
            )
            continue
        if _comparable(expected_item["value"]) != _comparable(actual_item["value"]):
            field = expected_item["field"]
            if actual_item["field"] != expected_item["field"]:
                field = f"{field}/{actual_item['field']}"
            mismatch = {
                "field": field,
                "expected": expected_item["value"],
                "actual": actual_item["value"],
                "kind": "identity_mismatch",
            }
            if _is_soft_identity_alias_diff(
                expected_item,
                actual_item,
                expected_server=expected_server,
                actual_server=actual_server,
            ):
                soft_diffs.append({**mismatch, "kind": "soft_identity_mismatch"})
            else:
                mismatches.append(mismatch)

    return mismatches, soft_diffs


def _non_identity_diffs(expected_args: Mapping[str, Any], actual_args: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected_scalars = {
        _canonical_path(path): {"field": ".".join(path), "value": value}
        for path, value in _flatten_scalars(expected_args)
        if path and not _is_identity_key(path[-1])
    }
    actual_scalars = {
        _canonical_path(path): {"field": ".".join(path), "value": value}
        for path, value in _flatten_scalars(actual_args)
        if path and not _is_identity_key(path[-1])
    }

    diffs: list[dict[str, Any]] = []
    for canonical, expected in expected_scalars.items():
        actual = actual_scalars.get(canonical)
        if actual is None:
            continue
        if _comparable(expected["value"]) == _comparable(actual["value"]):
            continue
        diffs.append(
            {
                "field": expected["field"],
                "expected": expected["value"],
                "actual": actual["value"],
                "kind": "non_identity_mismatch",
            }
        )
    return diffs


def _is_soft_identity_alias_diff(
    expected_item: Mapping[str, Any],
    actual_item: Mapping[str, Any],
    *,
    expected_server: str,
    actual_server: str,
) -> bool:
    if not _is_collaboration_alias_service(expected_server, actual_server):
        return False

    expected_field = _last_field_name(expected_item.get("field"))
    actual_field = _last_field_name(actual_item.get("field"))
    field_names = {expected_field, actual_field}
    expected_value = _comparable(expected_item.get("value"))
    actual_value = _comparable(actual_item.get("value"))

    if field_names == {"userid"}:
        return _is_me_email_alias_pair(expected_value, actual_value)
    if field_names == {"calendarid"}:
        return _is_primary_calendar_alias_pair(expected_value, actual_value)
    return False


def _is_collaboration_alias_service(*servers: str) -> bool:
    lowered_servers = [str(server or "").strip().lower() for server in servers]
    return any(
        service in server
        for server in lowered_servers
        for service in COLLABORATION_ALIAS_SERVICES
    )


def _last_field_name(field: Any) -> str:
    return str(field or "").rsplit(".", 1)[-1].replace("_", "").lower()


def _is_me_email_alias_pair(left: str, right: str) -> bool:
    values = {_alias_value(left), _alias_value(right)}
    return "me" in values and any(_looks_like_email(value) for value in values)


def _is_primary_calendar_alias_pair(left: str, right: str) -> bool:
    values = {_alias_value(left), _alias_value(right)}
    return "primary" in values and any(
        value != "primary" and _looks_like_primary_calendar_id(value) for value in values
    )


def _alias_value(value: str) -> str:
    return str(value or "").strip().lower()


def _looks_like_email(value: str) -> bool:
    left, separator, right = value.partition("@")
    return bool(left and separator and right)


def _looks_like_primary_calendar_id(value: str) -> bool:
    return value.endswith("-primary") or value.endswith("_primary") or value.endswith(":primary")


def _identity_evidence(
    action: str,
    comparison: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> dict[str, str]:
    trace_ref = str(actual.get("evidence") or "")
    if not trace_ref:
        return {}

    evidence: dict[str, str] = {}
    mismatched_fields = {
        str(item.get("field") or "").split("/", 1)[0]
        for item in comparison.get("identity_mismatches") or []
    }
    for item in (comparison.get("expected_identity") or {}).values():
        if str(item.get("field") or "") in mismatched_fields:
            continue
        marker = f"{item['field']}={item['value']}"
        evidence[f"{action}#object:{marker}"] = trace_ref
    return evidence


def _flatten_scalars(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        items: list[tuple[tuple[str, ...], Any]] = []
        for key, child in value.items():
            items.extend(_flatten_scalars(child, (*path, str(key))))
        return items
    if isinstance(value, list):
        items = []
        for index, child in enumerate(value):
            items.extend(_flatten_scalars(child, (*path, str(index))))
        return items
    return [(path, value)]


def _is_identity_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered == "id"
        or lowered.endswith("_id")
        or key.endswith("Id")
        or any(marker in lowered for marker in IDENTITY_MARKERS)
    )


def _canonical_path(path: tuple[str, ...]) -> str:
    return ".".join(part.replace("_", "").lower() for part in path)


def _comparable(value: Any) -> str:
    return str(value).strip()


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _match_tool_result(
    event: dict[str, Any],
    pending_by_id: dict[str, dict[str, Any]],
    pending_by_agent_tool: dict[tuple[str, str], deque[dict[str, Any]]],
) -> dict[str, Any] | None:
    call_id = _tool_call_id(event)
    if call_id and str(call_id) in pending_by_id:
        call = pending_by_id.pop(str(call_id))
        _remove_from_queue(call, pending_by_agent_tool)
        return call

    data = event.get("data") or {}
    agent = str(event.get("agent_name") or "")
    tool_name = _result_tool_name(data)
    if tool_name:
        queue = pending_by_agent_tool.get((agent, tool_name))
        if queue:
            call = queue.popleft()
            if call.get("tool_call_id"):
                pending_by_id.pop(str(call["tool_call_id"]), None)
            return call

    for (candidate_agent, _), queue in pending_by_agent_tool.items():
        if candidate_agent == agent and queue:
            call = queue.popleft()
            if call.get("tool_call_id"):
                pending_by_id.pop(str(call["tool_call_id"]), None)
            return call
    return None


def _remove_from_queue(
    call: dict[str, Any],
    pending_by_agent_tool: dict[tuple[str, str], deque[dict[str, Any]]],
) -> None:
    queue = pending_by_agent_tool.get((str(call.get("agent")), str(call.get("tool_name"))))
    if not queue:
        return
    try:
        queue.remove(call)
    except ValueError:
        return


def _is_failed_tool_result(event: dict[str, Any]) -> bool:
    data = event.get("data") or {}
    status = str(data.get("status") or "").strip().lower()
    if status in FAILED_STATUSES:
        return True
    if data.get("is_error") is True or data.get("error"):
        return True
    result = data.get("result") or data.get("content") or data.get("result_preview")
    if isinstance(result, Mapping):
        result_status = str(result.get("status") or "").strip().lower()
        return result_status in FAILED_STATUSES or bool(result.get("error"))
    return False


def _result_tool_name(data: Mapping[str, Any]) -> str:
    tool = data.get("tool_name") or data.get("name") or data.get("tool")
    if isinstance(tool, str) and "call_tool" not in tool and "call_knowledge_tool" not in tool:
        return tool
    arguments = data.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        return ""
    inner = arguments.get("tool_name") or arguments.get("name") or arguments.get("tool")
    return str(inner or "")


def _tool_call_id(event: Mapping[str, Any]) -> str:
    data = event.get("data") or {}
    if not isinstance(data, Mapping):
        data = {}
    for key in ("tool_call_id", "call_id", "id"):
        value = data.get(key) or event.get(key)
        if value:
            return str(value)
    return ""


def _step_action_label(step: Mapping[str, Any]) -> str:
    return f"{step.get('agent')}.{step.get('tool_name')}"


def _call_action_label(call: Mapping[str, Any]) -> str:
    return f"{call.get('agent')}.{call.get('tool_name')}"


def _evidence_ref(event: Mapping[str, Any], call: Mapping[str, Any]) -> str:
    request_id = event.get("request_id") or call.get("request_id") or ""
    ts = event.get("ts") or call.get("ts") or ""
    suffix = f"request_id={request_id}" if request_id else f"ts={ts}"
    return f"trace://entcollab/{call.get('agent')}/{call.get('tool_name')}?{suffix}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))
