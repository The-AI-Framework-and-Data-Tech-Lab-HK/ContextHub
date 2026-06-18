"""Closure checklist adapter for EntCollabBench traces.

This module is intentionally offline and pure Python: it does not import the
EntCollabBench runtime, call models, start Docker, or write ContextHub state.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import json
from typing import Any

from integrations.entcollabbench import closure_alignment


TIMEOUT_MARKERS = ("timeout", "timed out", "deadline", "cancelled")
FAILED_STATUSES = {"error", "failed", "failure", "timeout", "cancelled"}


def ground_truth_steps(dataset_task: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return EntCollabBench ground-truth steps from a task or subtask payload."""

    if isinstance(dataset_task, list):
        if all(_looks_like_ground_truth_step(item) for item in dataset_task):
            return list(dataset_task)
        steps: list[dict[str, Any]] = []
        for item in dataset_task:
            steps.extend(_ground_truth_steps_from_task(item))
        return steps
    if not isinstance(dataset_task, dict):
        return []
    return _ground_truth_steps_from_task(dataset_task)


def _ground_truth_steps_from_task(dataset_task: Any) -> list[dict[str, Any]]:
    if not isinstance(dataset_task, dict):
        return []
    if isinstance(dataset_task.get("ground_truth"), list):
        return list(dataset_task["ground_truth"])
    subtasks = dataset_task.get("sub_task_list") or []
    if not subtasks:
        return []
    steps: list[dict[str, Any]] = []
    for subtask in subtasks:
        if isinstance(subtask, dict):
            steps.extend(subtask.get("ground_truth") or [])
    return steps


def _looks_like_ground_truth_step(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return bool({"agent", "tool_name", "mcp_server_name"} & item.keys())


def required_actions_from_ground_truth(
    gt_steps: list[dict[str, Any]],
) -> list[str]:
    """Build WorkflowAnchor.required_actions as ``agent.tool_name`` labels."""

    return _unique(
        _action_label(step)
        for step in gt_steps
        if step.get("agent") and step.get("tool_name") and step.get("mcp_server_name")
    )


def actual_tool_call(event: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an EntCollabBench wrapped MCP tool call event."""

    data = event.get("data") or {}
    wrapper = str(data.get("tool_name") or "")
    if "call_tool" not in wrapper and "call_knowledge_tool" not in wrapper:
        return None
    arguments = data.get("arguments") or {}
    inner = arguments.get("tool_name") or arguments.get("name") or arguments.get("tool")
    if not inner:
        return None
    raw_args = arguments.get("arguments_json") or arguments.get("args") or {}
    if isinstance(raw_args, str):
        try:
            tool_args = json.loads(raw_args)
        except json.JSONDecodeError:
            tool_args = {"_raw_arguments_json": raw_args}
    elif isinstance(raw_args, dict):
        tool_args = dict(raw_args)
    else:
        tool_args = {}
    return {
        "agent": event.get("agent_name"),
        "server": _server_from_wrapper(wrapper),
        "wrapper_tool": wrapper,
        "tool_name": str(inner),
        "tool_args": tool_args,
        "event": event.get("event"),
        "request_id": event.get("request_id") or data.get("request_id"),
        "tool_call_id": _tool_call_id(event),
        "ts": event.get("ts"),
    }


def extract_completed_actions(
    trace_events: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    """Return successful completed actions and trace evidence.

    Completion requires a successful ``tool_result`` matched to a prior
    ``tool_call``. This avoids treating failed or timed-out tool results as
    completed workflow actions.
    """

    pending_by_id: dict[str, dict[str, Any]] = {}
    pending_by_agent_tool: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    calls_seen: list[dict[str, Any]] = []
    failed_results: list[dict[str, Any]] = []
    completed: list[str] = []
    evidence: dict[str, str] = {}

    for event in sorted(trace_events, key=lambda item: float(item.get("ts") or 0.0)):
        event_type = event.get("event")
        if event_type == "tool_call":
            call = actual_tool_call(event)
            if call is None:
                continue
            calls_seen.append(call)
            if call["tool_call_id"]:
                pending_by_id[str(call["tool_call_id"])] = call
            pending_by_agent_tool[(str(call["agent"]), str(call["tool_name"]))].append(call)
            continue

        if event_type != "tool_result":
            continue

        if _is_failed_tool_result(event):
            failed_results.append(_trace_ref(event))
            _discard_matching_call(event, pending_by_id, pending_by_agent_tool)
            continue

        call = _match_tool_result(event, pending_by_id, pending_by_agent_tool)
        if call is None:
            continue

        label = f"{call['agent']}.{call['tool_name']}"
        completed.append(label)
        evidence.setdefault(label, _evidence_ref(event, call))

    return _unique(completed), evidence, {
        "tool_calls_seen": calls_seen,
        "failed_tool_results": failed_results,
        "unmatched_tool_call_count": sum(len(queue) for queue in pending_by_agent_tool.values()),
    }


def build_workflow_closure_payload(
    *,
    workflow_id: str,
    ground_truth: list[dict[str, Any]] | dict[str, Any],
    trace_events: list[dict[str, Any]] | dict[str, Any],
    runtime_summary: dict[str, Any] | None = None,
    require_decision: bool = False,
    decision_label: str | None = None,
    rule_citations: list[str] | None = None,
) -> dict[str, Any]:
    """Build a ClosureGuardrail-compatible checklist plus adapter diagnostics."""

    gt_steps = ground_truth_steps(ground_truth)
    events = normalize_trace_events(trace_events)
    runtime = summarize_runtime_boundary(runtime_summary or {}, events)
    required_actions = required_actions_from_ground_truth(gt_steps)
    completed_actions, evidence, trace_diagnostics = extract_completed_actions(events)
    alignment_diagnostics = closure_alignment.align_ground_truth_to_trace(
        gt_steps,
        events,
        actual_tool_call=actual_tool_call,
    )
    evidence.update(alignment_diagnostics["evidence"])

    completed_counts = Counter(completed_actions)
    missing_actions: list[str] = []
    for action in required_actions:
        if completed_counts[action]:
            completed_counts[action] -= 1
        else:
            missing_actions.append(action)

    open_questions: list[str] = []
    if runtime["timeout"]:
        open_questions.append(
            "timeout_or_partial_trace: run ended before a clean terminal closure boundary"
        )
    for action in missing_actions:
        open_questions.append(f"missing_required_action: {action}")
    for diff in alignment_diagnostics["argument_diffs"]:
        action = diff["action"]
        for mismatch in diff["identity_mismatches"]:
            open_questions.append(
                "argument_mismatch: "
                f"{action} {mismatch['field']} {mismatch['expected']!r} != {mismatch['actual']!r}"
            )

    uncertainty: list[str] = []
    if runtime["timeout"]:
        uncertainty.append("timeout case lacks judge-level terminal pass/fail evidence")
    if trace_diagnostics["failed_tool_results"]:
        uncertainty.append("failed tool_result events were not counted as completed actions")
    if trace_diagnostics["unmatched_tool_call_count"]:
        uncertainty.append("some tool_call events had no matching successful tool_result")
    if alignment_diagnostics["misaligned_actions"]:
        uncertainty.append("some successful actions targeted different identity/object arguments")

    return {
        "anchor": {
            "workflow_id": workflow_id,
            "required_actions": required_actions,
            "required_evidence": [],
        },
        "completed_actions": completed_actions,
        "evidence": evidence,
        "open_questions": open_questions,
        "require_decision": require_decision,
        "decision_label": decision_label,
        "rule_citations": list(rule_citations) if rule_citations is not None else None,
        "diagnostics": {
            "missing_actions": missing_actions,
            "alignment": alignment_diagnostics,
            "runtime": runtime,
            "trace": trace_diagnostics,
            "uncertainty": uncertainty,
        },
    }


def build_approval_closure_payload(
    *,
    workflow_id: str,
    completed_actions: list[str] | None = None,
    evidence: dict[str, str] | None = None,
    open_questions: list[str] | None = None,
    required_actions: list[str] | None = None,
    required_evidence: list[str] | None = None,
    decision_label: str | None = None,
    rule_citations: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a closure payload for approval-subset workflows."""

    return {
        "anchor": {
            "workflow_id": workflow_id,
            "required_actions": list(required_actions or []),
            "required_evidence": list(required_evidence or []),
        },
        "completed_actions": list(completed_actions or []),
        "evidence": dict(evidence or {}),
        "open_questions": list(open_questions or []),
        "require_decision": True,
        "decision_label": decision_label,
        "rule_citations": list(rule_citations) if rule_citations is not None else None,
        "diagnostics": dict(diagnostics or {}),
    }


def build_closure_payload_from_baseline_case(
    case_summary: dict[str, Any],
    trajectory_row: dict[str, Any],
    dataset_task: dict[str, Any],
) -> dict[str, Any]:
    """Build a workflow closure checklist from existing baseline artifacts."""

    workflow_id = str(
        case_summary.get("case")
        or case_summary.get("task_id")
        or dataset_task.get("task_id")
        or "entcollab-workflow"
    )
    runtime_summary = {
        "status": case_summary.get("status"),
        "timeout": case_summary.get("timeout"),
        "failure_reason": case_summary.get("failure_reason"),
        "errors": case_summary.get("errors") or [],
        "failed_agents": case_summary.get("failed_agents") or [],
    }
    return build_workflow_closure_payload(
        workflow_id=workflow_id,
        ground_truth=dataset_task,
        trace_events=trajectory_row,
        runtime_summary=runtime_summary,
    )


def normalize_trace_events(trace_events: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    """Accept either a raw event list or an EntCollabBench trajectory row."""

    if isinstance(trace_events, list):
        return list(trace_events)
    if not isinstance(trace_events, dict):
        return []
    events: list[dict[str, Any]] = []
    task_results = trace_events.get("batch_entry", {}).get("task_results") or []
    if not task_results:
        return events
    traces = task_results[0].get("all_agent_traces") or {}
    for agent, wrapper in traces.items():
        for event in (wrapper.get("trace") or {}).get("events") or []:
            item = dict(event)
            item.setdefault("agent_name", agent)
            events.append(item)
    events.sort(key=lambda event: float(event.get("ts") or 0.0))
    return events


def summarize_runtime_boundary(
    runtime_summary: dict[str, Any],
    trace_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify timeout/partial-trace signals for closure diagnostics."""

    text_parts = [
        str(runtime_summary.get("status") or ""),
        str(runtime_summary.get("failure_reason") or ""),
        " ".join(str(item) for item in runtime_summary.get("errors") or []),
    ]
    for event in trace_events:
        if event.get("event") != "delegate_error":
            continue
        data = event.get("data") or {}
        text_parts.append(str(data.get("error") or data.get("result_preview") or ""))

    text = " ".join(text_parts).lower()
    timeout = bool(runtime_summary.get("timeout")) or any(marker in text for marker in TIMEOUT_MARKERS)
    partial_trace = timeout or any(event.get("event") == "delegate_error" for event in trace_events)

    return {
        "timeout": timeout,
        "partial_trace": partial_trace,
        "failure_reason": runtime_summary.get("failure_reason") or "",
        "failed_agents": list(runtime_summary.get("failed_agents") or []),
        "delegate_error_count": sum(1 for event in trace_events if event.get("event") == "delegate_error"),
    }


def _server_from_wrapper(wrapper: str) -> str:
    if not wrapper.startswith("mcp_"):
        return ""
    rest = wrapper.removeprefix("mcp_")
    if "_call_" in rest:
        return rest.split("_call_", 1)[0]
    if rest.endswith("_call_tool"):
        return rest.removesuffix("_call_tool")
    return rest.split("_", 1)[0]


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


def _discard_matching_call(
    event: dict[str, Any],
    pending_by_id: dict[str, dict[str, Any]],
    pending_by_agent_tool: dict[tuple[str, str], deque[dict[str, Any]]],
) -> None:
    _match_tool_result(event, pending_by_id, pending_by_agent_tool)


def _remove_from_queue(
    call: dict[str, Any],
    pending_by_agent_tool: dict[tuple[str, str], deque[dict[str, Any]]],
) -> None:
    queue = pending_by_agent_tool.get((str(call["agent"]), str(call["tool_name"])))
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
    if isinstance(result, dict):
        result_status = str(result.get("status") or "").strip().lower()
        return result_status in FAILED_STATUSES or bool(result.get("error"))
    return False


def _result_tool_name(data: dict[str, Any]) -> str:
    tool = data.get("tool_name") or data.get("name") or data.get("tool")
    if isinstance(tool, str) and "call_tool" not in tool and "call_knowledge_tool" not in tool:
        return tool
    arguments = data.get("arguments") or {}
    inner = arguments.get("tool_name") or arguments.get("name") or arguments.get("tool")
    return str(inner or "")


def _tool_call_id(event: dict[str, Any]) -> str:
    data = event.get("data") or {}
    for key in ("tool_call_id", "call_id", "id"):
        value = data.get(key) or event.get(key)
        if value:
            return str(value)
    return ""


def _action_label(step: dict[str, Any]) -> str:
    return f"{step.get('agent')}.{step.get('tool_name')}"


def _evidence_ref(event: dict[str, Any], call: dict[str, Any]) -> str:
    request_id = event.get("request_id") or call.get("request_id") or ""
    ts = event.get("ts") or call.get("ts") or ""
    suffix = f"request_id={request_id}" if request_id else f"ts={ts}"
    return f"trace://entcollab/{call['agent']}/{call['tool_name']}?{suffix}"


def _trace_ref(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    return {
        "ts": event.get("ts"),
        "agent": event.get("agent_name"),
        "event": event.get("event"),
        "tool": data.get("tool_name"),
        "status": data.get("status"),
        "error": data.get("error"),
    }


def _unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
