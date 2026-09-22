"""Offline S2 diagnostics for EntCollabBench baseline artifacts.

This module intentionally does not call models, Docker services, ContextHub DB,
or the external EntCollabBench runtime. It parses existing result/trajectory
JSONL artifacts and estimates which current ContextHub S2 guardrails would flag
execution boundaries once an adapter supplies approximate contracts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import argparse
import json
from pathlib import Path
import re
from typing import Any

from integrations.entcollabbench import closure_adapter, mapping


DEFAULT_ARTIFACT_DIR = Path(
    "/Users/sherrylin/Documents/PythonProjects/public/EntCollabBench/"
    "scripts/result/contexthub_baseline_cases"
)
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent

_CASE_RE = re.compile(r"\bCS-0*(?P<num>\d+)\b", re.IGNORECASE)
_CASE_ID_RE = re.compile(r"\bcase(?: ID)?\s+(?P<num>\d+)\b", re.IGNORECASE)
_ARTICLE_RE = re.compile(r"\b(?:article|knowledge article)\s+(?P<num>\d+)\b", re.IGNORECASE)
_USER_RE = re.compile(r"\bUSER_\d+\b")
_GROUP_RE = re.compile(r"\bGROUP_\d+\b")
_TEAM_RE = re.compile(r"\bteam_[A-Za-z0-9_]+\b")
_CHANNEL_RE = re.compile(r"\bchannel_[A-Za-z0-9_]+\b")


@dataclass
class BoundaryFlag:
    case: str
    boundary: str
    guardrail: str
    verdict: str
    violation_kind: str
    agent: str
    tool: str = ""
    event: str = ""
    detail: str = ""
    helps_real_failure: str = "unlikely"
    false_block_risk: str = "low"
    trace_event: dict[str, Any] = field(default_factory=dict)
    uncertainty: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "boundary": self.boundary,
            "guardrail": self.guardrail,
            "verdict": self.verdict,
            "violation_kind": self.violation_kind,
            "agent": self.agent,
            "tool": self.tool,
            "event": self.event,
            "detail": self.detail,
            "helps_real_failure": self.helps_real_failure,
            "false_block_risk": self.false_block_risk,
            "trace_event": self.trace_event,
            "uncertainty": self.uncertainty,
        }


def load_jsonl_one(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.loads(handle.readline())


def load_task_spec(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data[0]


def extract_object_ids(text: str) -> list[str]:
    """Best-effort object identifiers for handoff payloads."""

    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value not in seen:
            seen.add(value)
            found.append(value)

    for match in _CASE_RE.finditer(text or ""):
        add(f"case/{int(match.group('num'))}")
    for match in _CASE_ID_RE.finditer(text or ""):
        add(f"case/{int(match.group('num'))}")
    for match in _ARTICLE_RE.finditer(text or ""):
        add(f"knowledge/{int(match.group('num'))}")
    for pattern in (_USER_RE, _GROUP_RE, _TEAM_RE, _CHANNEL_RE):
        for match in pattern.finditer(text or ""):
            add(match.group(0))
    return found


def iter_trace_events(traj: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    task_results = traj.get("batch_entry", {}).get("task_results") or []
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


def ground_truth_steps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return closure_adapter.ground_truth_steps(spec)


def summarize_case_status(
    case: str,
    result: dict[str, Any],
    summary_row: dict[str, Any],
) -> dict[str, Any]:
    runtime = result.get("runtime_summary") or {}
    status = "passed" if result.get("task_passed") else "failed"
    if summary_row.get("timeout"):
        status = "timeout"
    return {
        "case": case,
        "task_id": result.get("task_id") or summary_row.get("task_id"),
        "description": summary_row.get("description") or runtime.get("description"),
        "status": status,
        "task_passed": bool(result.get("task_passed")),
        "failed_agents": list(summary_row.get("failed_agents") or []),
        "failure_reason": summary_row.get("failure_reason") or "",
        "tokens": {
            "run_input": int(result.get("run_input_tokens") or 0),
            "run_output": int(result.get("run_output_tokens") or 0),
            "run_total": int(result.get("run_total_tokens") or 0),
            "judge_total": int((result.get("judge") or {}).get("summary", {}).get("judge_total_tokens") or 0),
        },
        "trace_counts": dict(summary_row.get("trace_event_counts") or {}),
        "event_type_counts": dict(summary_row.get("event_type_by_agent") or {}),
    }


def actual_tool_call(event: dict[str, Any]) -> dict[str, Any] | None:
    return closure_adapter.actual_tool_call(event)


def _server_from_wrapper(wrapper: str) -> str:
    if not wrapper.startswith("mcp_"):
        return ""
    rest = wrapper.removeprefix("mcp_")
    if "_call_" in rest:
        return rest.split("_call_", 1)[0]
    if rest.endswith("_call_tool"):
        return rest.removesuffix("_call_tool")
    return rest.split("_", 1)[0]


def _required_missing(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    missing = []
    for name in sorted((expected.get("arguments") or {}).keys()):
        if name not in actual or actual.get(name) in (None, "", []):
            missing.append(name)
    return missing


def _value_mismatches(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    mismatches = []
    for name, expected_value in (expected.get("arguments") or {}).items():
        if name in actual and actual.get(name) != expected_value:
            mismatches.append(name)
    return sorted(mismatches)


def _schema_fields(expected: dict[str, Any]) -> dict[str, Any]:
    return mapping.to_tool_contract_fields(expected)


def diagnose_tool_calls(
    case: str,
    gt_steps: list[dict[str, Any]],
    events: list[dict[str, Any]],
    status: str,
) -> tuple[list[BoundaryFlag], list[dict[str, Any]]]:
    flags: list[BoundaryFlag] = []
    tool_records: list[dict[str, Any]] = []
    expected_by_agent_tool: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for step in gt_steps:
        if step.get("mcp_server_name"):
            expected_by_agent_tool[(step.get("agent"), step.get("tool_name"))].append(step)

    seen_expected: Counter[tuple[str, str]] = Counter()
    for event in events:
        if event.get("event") != "tool_call":
            continue
        call = actual_tool_call(event)
        if call is None:
            continue

        expected_options = expected_by_agent_tool.get((call["agent"], call["tool_name"]), [])
        expected = expected_options[min(seen_expected[(call["agent"], call["tool_name"])], len(expected_options) - 1)] if expected_options else None
        if expected is not None:
            seen_expected[(call["agent"], call["tool_name"])] += 1

        record = {
            **call,
            "matched_ground_truth": bool(expected),
            "expected_agent": expected.get("agent") if expected else "",
            "expected_server": expected.get("mcp_server_name") if expected else "",
            "missing_required_args": _required_missing(expected, call["tool_args"]) if expected else [],
            "value_mismatches": _value_mismatches(expected, call["tool_args"]) if expected else [],
            "contract": _schema_fields(expected) if expected else {},
        }
        tool_records.append(record)

        if not expected:
            continue

        if call["agent"] != expected.get("agent"):
            flags.append(
                BoundaryFlag(
                    case=case,
                    boundary="tool_call",
                    guardrail="tool_state",
                    verdict="block",
                    violation_kind="unauthorized_flow",
                    agent=str(call["agent"]),
                    tool=str(call["tool_name"]),
                    event="tool_call",
                    detail=f"actual agent differs from required role {expected.get('agent')}",
                    helps_real_failure="possible" if status != "passed" else "unlikely",
                    false_block_risk="high" if status == "passed" else "medium",
                    trace_event=_trace_ref(event),
                )
            )

        missing = record["missing_required_args"]
        if missing:
            flags.append(
                BoundaryFlag(
                    case=case,
                    boundary="tool_call",
                    guardrail="tool_state",
                    verdict="repair",
                    violation_kind="schema_or_enum",
                    agent=str(call["agent"]),
                    tool=str(call["tool_name"]),
                    event="tool_call",
                    detail=f"dataset-derived contract required args missing: {missing}",
                    helps_real_failure="unlikely",
                    false_block_risk="high" if status == "passed" else "medium",
                    trace_event=_trace_ref(event),
                    uncertainty=[
                        "dataset arguments are not full MCP schemas",
                        "wrapper/raw-service argument aliases may differ, e.g. body vs content",
                    ],
                )
            )

    expected_tool_keys = [
        (step.get("agent"), step.get("tool_name"))
        for step in gt_steps
        if step.get("mcp_server_name")
    ]
    actual_tool_keys = Counter((record["agent"], record["tool_name"]) for record in tool_records)
    for agent, tool in expected_tool_keys:
        if actual_tool_keys[(agent, tool)]:
            actual_tool_keys[(agent, tool)] -= 1
            continue
        flags.append(
            BoundaryFlag(
                case=case,
                boundary="closure",
                guardrail="closure",
                verdict="block",
                violation_kind="unclosed_workflow",
                agent=str(agent),
                tool=str(tool),
                event="missing_expected_tool_call",
                detail=f"required ground-truth action was not observed: {agent}.{tool}",
                helps_real_failure="likely" if status != "passed" else "possible",
                false_block_risk="low" if status != "passed" else "high",
                uncertainty=["requires adapter-generated closure checklist or timeout recovery boundary"],
            )
        )
    return flags, tool_records


def diagnose_handoffs(
    case: str,
    events: list[dict[str, Any]],
    status: str,
) -> tuple[list[BoundaryFlag], list[dict[str, Any]]]:
    flags: list[BoundaryFlag] = []
    records: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") not in {"delegate_start", "delegate_done", "delegate_error"}:
            continue
        data = event.get("data") or {}
        task = data.get("task") or data.get("result_preview") or data.get("error") or ""
        sender = data.get("from_agent") or event.get("agent_name")
        recipient = data.get("to_agent") or data.get("handled_by") or ""
        packet = {
            "sender": sender,
            "recipient": recipient,
            "task_intent": task if event.get("event") == "delegate_start" else "",
            "expected_action": "complete delegated task" if event.get("event") == "delegate_start" else "",
            "required_object_ids": extract_object_ids(task),
            "context_versions": [mapping.role_uri(recipient)] if recipient else [],
        }
        missing = [
            key
            for key in ("sender", "recipient", "task_intent", "expected_action", "required_object_ids")
            if not packet.get(key)
        ]
        record = {
            "event": event.get("event"),
            "agent": event.get("agent_name"),
            "sender": sender,
            "recipient": recipient,
            "packet": packet,
            "missing_fields": missing,
            "status": data.get("status"),
        }
        records.append(record)

        if missing:
            flags.append(
                BoundaryFlag(
                    case=case,
                    boundary="handoff",
                    guardrail="handoff",
                    verdict="repair",
                    violation_kind="incomplete_handoff",
                    agent=str(sender),
                    event=str(event.get("event")),
                    detail=f"handoff packet missing fields: {missing}",
                    helps_real_failure="possible" if event.get("event") == "delegate_error" else "unlikely",
                    false_block_risk="medium" if status == "passed" else "low",
                    trace_event=_trace_ref(event),
                    uncertainty=[
                        "delegate_done/error are completion records, not necessarily pre-handoff packets",
                        "required_object_ids are regex-derived from natural-language task/result text",
                    ],
                )
            )
        if event.get("event") == "delegate_error":
            flags.append(
                BoundaryFlag(
                    case=case,
                    boundary="handoff",
                    guardrail="handoff",
                    verdict="allow",
                    violation_kind="none",
                    agent=str(sender),
                    event="delegate_error",
                    detail="existing handoff guardrail does not directly classify provider/runtime timeout",
                    helps_real_failure="unlikely",
                    false_block_risk="low",
                    trace_event=_trace_ref(event),
                    uncertainty=["timeout/error needs adapter timeout boundary or closure checklist to become actionable"],
                )
            )
    return flags, records


def diagnose_closure(
    case: str,
    gt_steps: list[dict[str, Any]],
    events: list[dict[str, Any]],
    status: str,
    tool_records: list[dict[str, Any]],
) -> tuple[list[BoundaryFlag], dict[str, Any]]:
    checklist = closure_adapter.build_workflow_closure_payload(
        workflow_id=case,
        ground_truth=gt_steps,
        trace_events=events,
        runtime_summary={"status": status, "timeout": status == "timeout"},
    )
    required_actions = list(checklist["anchor"]["required_actions"])
    completed = list(checklist["completed_actions"])

    final_outputs = []
    for event in events:
        if event.get("event") != "agent_message":
            continue
        content = ((event.get("data") or {}).get("message") or {}).get("content")
        if isinstance(content, str) and _looks_like_final(content):
            final_outputs.append(
                {
                    "agent": event.get("agent_name"),
                    "preview": content[:240],
                    "ts": event.get("ts"),
                }
            )

    missing = list(checklist["diagnostics"]["missing_actions"])
    flags: list[BoundaryFlag] = []
    if missing or checklist["diagnostics"]["runtime"]["timeout"]:
        detail = f"missing closure actions: {missing}" if missing else "run timed out before a clean terminal closure"
        flags.append(
            BoundaryFlag(
                case=case,
                boundary="closure",
                guardrail="closure",
                verdict="block",
                violation_kind="unclosed_workflow",
                agent="collaboration_ops_specialist",
                event="final_or_timeout",
                detail=detail,
                helps_real_failure="likely" if checklist["diagnostics"]["runtime"]["timeout"] else "possible",
                false_block_risk="low" if status != "passed" else "high",
                uncertainty=[
                    "adapter-generated closure checklist uses trace tool_result completion evidence",
                    *checklist["diagnostics"]["uncertainty"],
                ],
            )
        )
    return flags, {
        "required_actions": required_actions,
        "completed_actions": completed,
        "missing_actions": missing,
        "final_outputs": final_outputs[-5:],
        "checklist": checklist,
        "diagnostics": checklist["diagnostics"],
    }


def _action_label(step: dict[str, Any]) -> str:
    return f"{step.get('agent')}.{step.get('tool_name')}"


def _looks_like_final(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "done:",
            "all tasks completed",
            "completed successfully",
            "error:",
            "undone:",
        )
    )


def _trace_ref(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    return {
        "ts": event.get("ts"),
        "agent": event.get("agent_name"),
        "event": event.get("event"),
        "tool": data.get("tool_name"),
        "to_agent": data.get("to_agent") or data.get("handled_by"),
        "status": data.get("status"),
    }


def diagnose_artifacts(artifact_dir: Path = DEFAULT_ARTIFACT_DIR) -> dict[str, Any]:
    analysis = json.loads((artifact_dir / "analysis_summary.json").read_text(encoding="utf-8"))
    summary_by_case = {row["case"]: row for row in analysis}
    case_summaries = []
    all_flags: list[BoundaryFlag] = []
    cases = []

    for row in analysis:
        case = row["case"]
        result = load_jsonl_one(artifact_dir / f"{case}_result.jsonl")
        traj = load_jsonl_one(artifact_dir / f"{case}_traj.jsonl")
        spec = load_task_spec(artifact_dir / f"{case}.json")
        status = summarize_case_status(case, result, row)
        events = iter_trace_events(traj)
        gt_steps = ground_truth_steps(spec)
        handoff_flags, handoff_records = diagnose_handoffs(case, events, status["status"])
        tool_flags, tool_records = diagnose_tool_calls(case, gt_steps, events, status["status"])
        closure_flags, closure_record = diagnose_closure(
            case,
            gt_steps,
            events,
            status["status"],
            tool_records,
        )
        flags = handoff_flags + tool_flags + closure_flags
        all_flags.extend(flags)
        case_summaries.append(status)
        cases.append(
            {
                "case": case,
                "s0": status,
                "ground_truth_step_count": len(gt_steps),
                "handoff_candidates": handoff_records,
                "tool_call_candidates": tool_records,
                "closure_candidate": closure_record,
                "s2_diagnostic_flags": [flag.to_json() for flag in flags],
            }
        )

    totals = Counter(
        (flag.guardrail, flag.verdict, flag.violation_kind)
        for flag in all_flags
        if flag.violation_kind != "none"
    )
    return {
        "artifact_dir": str(artifact_dir),
        "case_count": len(cases),
        "s0_summary": case_summaries,
        "flag_totals": [
            {
                "guardrail": guardrail,
                "verdict": verdict,
                "violation_kind": kind,
                "count": count,
            }
            for (guardrail, verdict, kind), count in sorted(totals.items())
        ],
        "cases": cases,
        "claims": {
            "contextHub_can_claim": [
                "missing closure evidence/actions if adapter emits a terminal or timeout closure checklist",
                "schema/role violations on tool calls when runtime tool schemas and contracts are available",
            ],
            "contextHub_should_not_claim": [
                "model planning loops",
                "provider latency and task-level timeout by itself",
                "knowledge-only tool exploration errors unless represented as policy/tool contracts",
            ],
        },
        "adapter_gaps": [
            "Teams export_state/database-state fallback for deterministic mutation evidence",
            "runtime MCP tool schema extraction instead of dataset-argument pseudo schemas",
            "tool wrapper argument normalization, especially Teams body/content aliases",
            "closure checklist generation from ground truth, canonical diff, and trace results",
            "handoff packet generation with stable object IDs and context_versions",
            "timeout recovery boundary that converts delegate_error/partial trace into closure diagnostics",
        ],
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# ContextHub × EntCollabBench S2 Offline Diagnostic",
        "",
        "This report is generated only from existing baseline result/trajectory artifacts. "
        "No model benchmark, Docker service, ContextHub DB write, or core guardrail change was used.",
        "",
        "## Executive Conclusion",
        "",
        "- The strongest S2 signal is closure: the two timeout cases reached a KB delegation/error path without clean completion, so a timeout-aware closure checklist would likely flag real unfinished work.",
        "- Existing handoff guardrails mostly verify packet completeness, ACL, and stale refs; they do not directly detect provider/runtime timeout, so timeout failures should not be claimed as handoff policy wins without adapter support.",
        "- Tool-state can catch role/schema/object/provenance problems, but dataset-derived pseudo schemas create false-repair risk on already-passing S0 cases, especially service wrapper argument aliases.",
        "",
        "## S0 Baseline Cases",
        "",
        "| Case | S0 status | Failed agents | Tokens | Trace events |",
        "|---|---:|---|---:|---:|",
    ]
    for case in summary["s0_summary"]:
        trace_total = sum(int(v or 0) for v in case["trace_counts"].values())
        failed = ", ".join(case["failed_agents"]) or "-"
        lines.append(
            f"| {case['case']} | {case['status']} | {failed} | "
            f"{case['tokens']['run_total']} | {trace_total} |"
        )

    lines.extend(["", "## S2 Diagnostic Flags", ""])
    for case in summary["cases"]:
        lines.append(f"### {case['case']}")
        lines.append(
            f"- S0: {case['s0']['status']}; tokens={case['s0']['tokens']['run_total']}; "
            f"failure={case['s0']['failure_reason'] or '-'}"
        )
        trace_counts = {
            agent: count
            for agent, count in case["s0"]["trace_counts"].items()
            if int(count or 0) > 0
        }
        lines.append(
            "- Trace counts by agent: "
            + (", ".join(f"`{agent}`={count}" for agent, count in trace_counts.items()) or "-")
        )
        flags = case["s2_diagnostic_flags"]
        if not flags:
            lines.append("- No diagnostic flags under the offline approximation.")
            continue
        for flag in flags:
            lines.append(
                f"- `{flag['boundary']}` / `{flag['guardrail']}` -> `{flag['verdict']}` "
                f"({flag['violation_kind']}), agent=`{flag['agent']}`, "
                f"tool=`{flag['tool'] or '-'}`, event=`{flag['event']}`. "
                f"Help: {flag['helps_real_failure']}; false-block risk: {flag['false_block_risk']}. "
                f"{flag['detail']}"
            )

    lines.extend(
        [
            "",
            "## Adapter Gaps",
            "",
            *[f"- {item}" for item in summary["adapter_gaps"]],
            "",
            "## Claim Boundaries",
            "",
            "ContextHub can reasonably claim failures that are expressed as missing closure actions/evidence, "
            "contract/schema/role/object/provenance violations, or stale/blocked context dependencies.",
            "",
            "ContextHub should not claim raw model planning loops, provider latency, or task timeout by itself. "
            "Those become S2-relevant only when the adapter turns the partial trace into a closure or timeout boundary with explicit unmet obligations.",
            "",
            "## Recommended Next Step",
            "",
            "Prioritize adapter work before online S2: closure checklist generation and tool schema/argument normalization will reduce both missed flags and false repairs. "
            "After that, run online S2 first on `mcp_single_145` and `mcp_single_146`: the former exercises timeout/KB failure, while the latter is a short passing case for false-block detection.",
            "",
            "## Risks And Uncertainty",
            "",
            "- Tool contracts here are inferred from dataset ground truth, not live MCP `inputSchema`; this can overstate schema violations.",
            "- Handoff object IDs are regex-derived from natural-language trace text and need a real object mapping/export-state adapter.",
            "- The timeout cases lack judge-level fine-grained pass/fail labels because judge did not run after timeout.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    summary = diagnose_artifacts(artifact_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "s2_diagnostic_summary.json"
    md_path = output_dir / "S2_DIAGNOSTIC_REPORT.md"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_markdown(summary, md_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    write_outputs(args.artifact_dir, args.output_dir)


if __name__ == "__main__":
    main()
