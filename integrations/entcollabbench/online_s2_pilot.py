"""Near-online S2 pilot diagnostics for fixed EntCollabBench cases.

The pilot intentionally works from fixed task specs plus existing result and
trajectory artifacts. It can query live MCP tool schemas, but it does not call
models, run benchmark tasks, write ContextHub DB state, or modify the external
EntCollabBench checkout.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import GuardrailDecision, Verdict
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.models.request import RequestContext

from integrations.entcollabbench import closure_adapter, tool_contract_adapter
from integrations.entcollabbench.mcp_runtime_adapter import (
    McpEndpointConfig,
    McpRuntimeAdapterError,
    get_tool_schema,
    normalize_tool_schema_record,
)


DEFAULT_EXTERNAL_ROOT = Path("/Users/sherrylin/Documents/PythonProjects/research/EntCollabBench")
DEFAULT_BASELINE_DIR = DEFAULT_EXTERNAL_ROOT / "scripts/result/contexthub_baseline_cases"
DEFAULT_SPEC_DIR = DEFAULT_EXTERNAL_ROOT / "scripts/result/contexthub_online_s2_cases"
DEFAULT_DATASET = DEFAULT_EXTERNAL_ROOT / "scripts/dataset/mcp_tasks_160.json"
DEFAULT_ENDPOINTS = DEFAULT_EXTERNAL_ROOT / "config/mcp_endpoints_export.json"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent
DEFAULT_CASES = ("mcp_single_146", "mcp_single_145")

SchemaProvider = Callable[[str, str], dict[str, Any]]


class NoopStaleness:
    async def any_stale_or_blocked_refs(self, db, refs):
        return []


def ensure_fixed_case_specs(
    *,
    dataset_path: Path = DEFAULT_DATASET,
    output_dir: Path = DEFAULT_SPEC_DIR,
    cases: tuple[str, ...] = DEFAULT_CASES,
) -> list[Path]:
    """Write deterministic one-case task spec files for the pilot cases."""

    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    by_task = {str(item.get("task_id")): item for item in dataset}
    paths: list[Path] = []
    for case in cases:
        if case not in by_task:
            raise KeyError(f"{case} not found in {dataset_path}")
        path = output_dir / f"{case}.json"
        path.write_text(json.dumps([by_task[case]], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


async def run_pilot(
    *,
    cases: tuple[str, ...] = DEFAULT_CASES,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    spec_dir: Path = DEFAULT_SPEC_DIR,
    endpoint_config_path: Path | None = DEFAULT_ENDPOINTS,
    schema_provider: SchemaProvider | None = None,
) -> dict[str, Any]:
    """Run the near-online S2 diagnostic over fixed case artifacts."""

    analysis = json.loads((baseline_dir / "analysis_summary.json").read_text(encoding="utf-8"))
    analysis_by_case = {row["case"]: row for row in analysis}
    endpoint_config = None
    if endpoint_config_path is not None and endpoint_config_path.exists():
        endpoint_config = McpEndpointConfig.from_file(endpoint_config_path)

    schema_cache: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    case_results = []
    for case in cases:
        spec = _load_task_spec(spec_dir / f"{case}.json")
        result = _load_jsonl_one(baseline_dir / f"{case}_result.jsonl")
        trajectory = _load_jsonl_one(baseline_dir / f"{case}_traj.jsonl")
        row = analysis_by_case[case]
        status = _case_status(case, result, row)
        events = closure_adapter.normalize_trace_events(trajectory)
        ground_truth = closure_adapter.ground_truth_steps(spec)

        closure_payload = closure_adapter.build_workflow_closure_payload(
            workflow_id=case,
            ground_truth=ground_truth,
            trace_events=events,
            runtime_summary={
                "status": status["status"],
                "timeout": status["timeout"],
                "failure_reason": status["failure_reason"],
                "errors": status["errors"],
                "failed_agents": status["failed_agents"],
            },
        )
        closure_decision = await _closure_decision(case, closure_payload)

        tool_decisions = []
        for event in events:
            if event.get("event") != "tool_call":
                continue
            call = closure_adapter.actual_tool_call(event)
            if call is None:
                continue

            schema, schema_source = _schema_for_call(
                call,
                endpoint_config=endpoint_config,
                schema_provider=schema_provider,
                schema_cache=schema_cache,
            )
            normalized_args = tool_contract_adapter.normalize_tool_args(
                call["server"],
                call["tool_name"],
                call["tool_args"],
            )
            contract = tool_contract_adapter.tool_schema_to_contract_fields(
                call["server"],
                schema,
                required_role=call["agent"],
                mutation_intent=_mutation_intent(call["tool_name"]),
            )
            contract["arg_schema"] = _guardrail_compatible_schema(contract["arg_schema"])
            decision = await _tool_state_decision(call["agent"], contract, normalized_args)
            tool_decisions.append(
                {
                    "agent": call["agent"],
                    "server": call["server"],
                    "wrapper_tool": call["wrapper_tool"],
                    "tool_name": call["tool_name"],
                    "schema_source": schema_source,
                    "schema_required": list(contract["arg_schema"].get("required") or []),
                    "normalized_arg_keys": sorted(str(key) for key in normalized_args.keys()),
                    "decision": _decision_json(decision),
                }
            )

        case_results.append(
            {
                "case": case,
                "mode": "near-online/post-run S2 diagnostic",
                "s0": status,
                "ground_truth_required_actions": closure_payload["anchor"]["required_actions"],
                "closure": {
                    "payload": _closure_payload_summary(closure_payload),
                    "decision": _decision_json(closure_decision),
                },
                "tool_state": {
                    "decision_counts": _decision_counts(tool_decisions),
                    "repair_or_block_count": sum(
                        1
                        for item in tool_decisions
                        if item["decision"]["verdict"] in {"repair", "block", "escalate"}
                    ),
                    "calls": tool_decisions,
                },
            }
        )

    return {
        "mode": "near-online/post-run S2 diagnostic",
        "benchmark_rerun": False,
        "cases": case_results,
        "claim_boundary": {
            "online": False,
            "closure": "ClosureGuardrail evaluated post-run closure payloads from fixed specs and trajectories.",
            "tool_call": "ToolStateGuardrail evaluated observed tool_call events with live schema when available and normalized args.",
            "handoff": "No runtime handoff interception was inserted into the external EntCollabBench agent loop.",
        },
    }


def write_outputs(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "online_s2_pilot_summary.json"
    report_path = output_dir / "ONLINE_S2_PILOT_REPORT.md"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.write_text(_markdown_report(summary), encoding="utf-8")
    return json_path, report_path


def _load_jsonl_one(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.loads(handle.readline())


def _load_task_spec(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data[0]
    return data


def _case_status(case: str, result: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    status = "passed" if result.get("task_passed") else "failed"
    if row.get("timeout"):
        status = "timeout"
    return {
        "case": case,
        "task_id": result.get("task_id") or row.get("task_id"),
        "description": row.get("description") or (result.get("runtime_summary") or {}).get("description"),
        "status": status,
        "task_passed": bool(result.get("task_passed")),
        "timeout": bool(row.get("timeout")),
        "failed_agents": list(row.get("failed_agents") or []),
        "failure_reason": row.get("failure_reason") or "",
        "errors": list(row.get("errors") or []),
        "tokens": {
            "run_total": int(result.get("run_total_tokens") or 0),
            "judge_total": int((result.get("judge") or {}).get("summary", {}).get("judge_total_tokens") or 0),
        },
        "trace_event_counts": dict(row.get("trace_event_counts") or {}),
    }


async def _closure_decision(case: str, payload: dict[str, Any]) -> GuardrailDecision:
    ec = EnforcementContext(
        boundary=Boundary.CLOSURE,
        actor=RequestContext(account_id="entcollab-pilot", agent_id="collaboration_ops_specialist"),
        payload=payload,
        workflow_id=case,
    )
    return await ClosureGuardrail(NoopStaleness()).check(None, ec)


async def _tool_state_decision(
    agent: str,
    contract: dict[str, Any],
    args: dict[str, Any],
) -> GuardrailDecision:
    ec = EnforcementContext(
        boundary=Boundary.TOOL_CALL,
        actor=RequestContext(account_id="entcollab-pilot", agent_id=agent),
        payload={"contract": contract, "tool_args": args},
        declared_context_uris=list(contract.get("depends_on_uris") or []),
    )
    return await ToolStateGuardrail(NoopStaleness()).check(None, ec)


def _schema_for_call(
    call: dict[str, Any],
    *,
    endpoint_config: McpEndpointConfig | None,
    schema_provider: SchemaProvider | None,
    schema_cache: dict[tuple[str, str], tuple[dict[str, Any], str]],
) -> tuple[dict[str, Any], str]:
    key = (str(call["server"]), str(call["tool_name"]))
    if key in schema_cache:
        return schema_cache[key]

    if schema_provider is not None:
        schema = normalize_tool_schema_record(schema_provider(*key), tool_name=key[1])
        schema_cache[key] = (schema, "injected-live-schema")
        return schema_cache[key]

    if endpoint_config is not None and key[0]:
        try:
            schema = get_tool_schema(endpoint_config, key[0], key[1])
            schema_cache[key] = (schema, "live-mcp-schema")
            return schema_cache[key]
        except (McpRuntimeAdapterError, OSError, TimeoutError, ValueError) as exc:
            fallback = normalize_tool_schema_record({"name": key[1]})
            schema_cache[key] = (fallback, f"schema-unavailable:{type(exc).__name__}")
            return schema_cache[key]

    fallback = normalize_tool_schema_record({"name": key[1]})
    schema_cache[key] = (fallback, "schema-unavailable:no-endpoint-config")
    return schema_cache[key]


def _mutation_intent(tool_name: str) -> str:
    name = str(tool_name).lower()
    if name.startswith(("update", "set", "close", "resolve")):
        return "update"
    if name.startswith(("create", "send", "post", "add")):
        return "create"
    return ""


def _guardrail_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert live JSON Schema details to the subset ToolStateGuardrail accepts."""

    normalized = dict(schema)
    properties = {}
    for name, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            properties[name] = spec
            continue
        item = dict(spec)
        schema_type = item.get("type")
        if isinstance(schema_type, list):
            first_supported = next(
                (
                    value
                    for value in schema_type
                    if value in {"string", "integer", "number", "boolean", "array", "object"}
                ),
                None,
            )
            if first_supported is None:
                item.pop("type", None)
            else:
                item["type"] = first_supported
        properties[name] = item
    normalized["properties"] = properties
    return normalized


def _decision_json(decision: GuardrailDecision) -> dict[str, Any]:
    return {
        "guardrail": decision.guardrail,
        "verdict": decision.verdict.value if isinstance(decision.verdict, Verdict) else str(decision.verdict),
        "reason": decision.reason,
        "violations": [
            {
                "kind": violation.kind.value,
                "message": violation.message,
                "repair_hint": violation.repair_hint,
                "evidence": violation.evidence,
            }
            for violation in decision.violations
        ],
    }


def _decision_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(item["decision"]["verdict"] for item in records)
    return dict(sorted(counts.items()))


def _closure_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    diagnostics = payload.get("diagnostics") or {}
    alignment = diagnostics.get("alignment") or {}
    argument_diffs = list(alignment.get("argument_diffs") or [])
    identity_mismatch_count = sum(
        len(diff.get("identity_mismatches") or []) for diff in argument_diffs
    )
    soft_identity_diff_count = sum(
        len(diff.get("soft_identity_diffs") or []) for diff in argument_diffs
    )
    return {
        "completed_actions": payload["completed_actions"],
        "missing_actions": diagnostics.get("missing_actions") or [],
        "alignment_misaligned_actions": list(alignment.get("misaligned_actions") or []),
        "alignment_argument_diff_count": len(argument_diffs),
        "alignment_soft_identity_diff_count": soft_identity_diff_count,
        "alignment_identity_mismatch_count": identity_mismatch_count,
        "open_questions": payload["open_questions"],
        "runtime": diagnostics.get("runtime") or {},
        "uncertainty": diagnostics.get("uncertainty") or [],
    }


def _markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# ContextHub × EntCollabBench Online S2 Pilot",
        "",
        "This report uses fixed case specs and existing baseline artifacts for a near-online/post-run S2 diagnostic. "
        "No benchmark rerun, model call, ContextHub DB write, core guardrail change, or external EntCollabBench source edit was performed.",
        "",
        "## Execution Mode",
        "",
        f"- Benchmark rerun: {'yes' if summary['benchmark_rerun'] else 'no'}",
        f"- Diagnostic mode: {summary['mode']}",
        "- Tool-state contract source: live MCP schema when available, with normalized observed args; schema lookup failures are recorded per call.",
        "",
        "## Case Results",
        "",
    ]
    by_case = {case["case"]: case for case in summary["cases"]}
    ordered_case_names = [case_name for case_name in DEFAULT_CASES if case_name in by_case]
    ordered_case_names.extend(
        case_name for case_name in by_case if case_name not in ordered_case_names
    )
    for case_name in ordered_case_names:
        case = by_case[case_name]
        closure = case["closure"]
        tool_state = case["tool_state"]
        missing = closure["payload"]["missing_actions"]
        misaligned = closure["payload"].get("alignment_misaligned_actions") or []
        lines.extend(
            [
                f"### {case_name}",
                f"- S0 status: {case['s0']['status']}; passed={case['s0']['task_passed']}; tokens={case['s0']['tokens']['run_total']}",
                f"- Closure decision: `{closure['decision']['verdict']}`; missing_actions={missing or []}",
                "- Closure alignment: "
                f"misaligned_actions={misaligned or []}; "
                f"argument_diffs={closure['payload'].get('alignment_argument_diff_count', 0)}; "
                f"identity_mismatches={closure['payload'].get('alignment_identity_mismatch_count', 0)}; "
                f"soft_identity_diffs={closure['payload'].get('alignment_soft_identity_diff_count', 0)}",
                f"- Tool-state decisions: {tool_state['decision_counts']}; repair_or_block_count={tool_state['repair_or_block_count']}",
            ]
        )
        if case_name == "mcp_single_146":
            lines.append(
                "- False-block readout: no S2 block is expected for a passed case if closure is `allow` and live-schema tool_state has no repair/block."
            )
        if case_name == "mcp_single_145":
            lines.append(
                "- Timeout/KB readout: closure should block when timeout/partial trace leaves `knowledge_base_specialist.update_knowledge` incomplete."
            )
        if case["s0"]["failure_reason"]:
            lines.append(f"- S0 failure reason: {case['s0']['failure_reason']}")
        if closure["payload"]["open_questions"]:
            lines.append(f"- Closure open questions: {closure['payload']['open_questions']}")
        lines.append("")

    lines.extend(
        [
            "## Tool-State False Positive Risk",
            "",
            "Live schema plus argument normalization avoids the dataset pseudo-schema problem where wrapper aliases such as Teams `content`/`body` create false repairs. Remaining tool_state repairs in this report should be read as live-schema validation findings, not ground-truth argument-diff findings.",
            "",
            "## Online Boundary",
            "",
            "- Closure: evaluated through `ClosureGuardrail` on adapter-built post-run payloads, not inserted into the agent runtime close path.",
            "- Tool call: evaluated through `ToolStateGuardrail` on observed trace events, not before the external runtime executed each tool.",
            "- Handoff: not intercepted in the external agent loop in this pilot.",
            "",
            "## Engineering Next Steps",
            "",
            "- Add real runtime hooks around EntCollabBench agent handoff, tool_call, and closure boundaries in a ContextHub-owned wrapper before claiming full online interception.",
            "- Keep live MCP schema extraction and wrapper argument normalization in the online path to reduce false repairs on passing cases.",
            "- Add timeout recovery that emits a closure boundary with unmet required actions, especially missing KB mutations such as `knowledge_base_specialist.update_knowledge`.",
        ]
    )
    return "\n".join(lines) + "\n"


async def _async_main(args: argparse.Namespace) -> None:
    ensure_fixed_case_specs(dataset_path=args.dataset, output_dir=args.spec_dir, cases=tuple(args.cases))
    summary = await run_pilot(
        cases=tuple(args.cases),
        baseline_dir=args.baseline_dir,
        spec_dir=args.spec_dir,
        endpoint_config_path=args.endpoint_config,
    )
    json_path, report_path = write_outputs(output_dir=args.output_dir, summary=summary)
    print(f"summary={json_path}")
    print(f"report={report_path}")
    for case in summary["cases"]:
        print(
            f"{case['case']}: closure={case['closure']['decision']['verdict']} "
            f"tool_state={case['tool_state']['decision_counts']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    parser.add_argument("--endpoint-config", type=Path, default=DEFAULT_ENDPOINTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cases", nargs="+", default=list(DEFAULT_CASES))
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
