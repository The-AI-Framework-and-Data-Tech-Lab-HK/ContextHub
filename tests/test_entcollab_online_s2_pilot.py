from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.entcollabbench.online_s2_pilot import (
    _markdown_report,
    ensure_fixed_case_specs,
    run_pilot,
)


def test_ensure_fixed_case_specs_writes_single_case_files(tmp_path: Path) -> None:
    dataset = tmp_path / "mcp_tasks_160.json"
    out_dir = tmp_path / "cases"
    dataset.write_text(
        json.dumps(
            [
                {"task_id": "mcp_single_145", "description": "case 145"},
                {"task_id": "mcp_single_146", "description": "case 146"},
            ]
        ),
        encoding="utf-8",
    )

    paths = ensure_fixed_case_specs(
        dataset_path=dataset,
        output_dir=out_dir,
        cases=("mcp_single_146", "mcp_single_145"),
    )

    assert [path.name for path in paths] == ["mcp_single_146.json", "mcp_single_145.json"]
    assert json.loads(paths[0].read_text(encoding="utf-8")) == [
        {"task_id": "mcp_single_146", "description": "case 146"}
    ]


@pytest.mark.asyncio
async def test_run_pilot_allows_passed_case_with_live_schema_alias_normalization(
    tmp_path: Path,
) -> None:
    _write_artifacts(
        tmp_path,
        case="mcp_single_146",
        passed=True,
        timeout=False,
        ground_truth=[
            {
                "mcp_server_name": "teams",
                "tool_name": "send_channel_message",
                "agent": "collaboration_ops_specialist",
                "arguments": {"teamId": "team_techcorp_001", "body": {"content": "hello"}},
            }
        ],
        events=[
            _tool_call(
                "collaboration_ops_specialist",
                "mcp_teams_call_tool",
                "send_channel_message",
                '{"teamId": "team_techcorp_001", "content": "hello"}',
                "call-1",
                1.0,
            ),
            _tool_result("collaboration_ops_specialist", "call-1", 2.0),
        ],
    )

    summary = await run_pilot(
        cases=("mcp_single_146",),
        baseline_dir=tmp_path,
        spec_dir=tmp_path,
        endpoint_config_path=None,
        schema_provider=_schema_provider,
    )

    case = summary["cases"][0]
    assert case["closure"]["decision"]["verdict"] == "allow"
    assert case["closure"]["payload"]["missing_actions"] == []
    assert case["tool_state"]["decision_counts"] == {"allow": 1}
    assert case["tool_state"]["repair_or_block_count"] == 0


@pytest.mark.asyncio
async def test_run_pilot_blocks_timeout_missing_update_knowledge(tmp_path: Path) -> None:
    _write_artifacts(
        tmp_path,
        case="mcp_single_145",
        passed=False,
        timeout=True,
        ground_truth=[
            {
                "mcp_server_name": "csm",
                "tool_name": "update_case",
                "agent": "customer_support_specialist",
                "arguments": {"case_id": "CS-1"},
            },
            {
                "mcp_server_name": "csm",
                "tool_name": "update_knowledge",
                "agent": "knowledge_base_specialist",
                "arguments": {"knowledge_id": "KB-1"},
            },
        ],
        events=[
            _tool_call(
                "customer_support_specialist",
                "mcp_csm_call_tool",
                "update_case",
                '{"case_id": "CS-1"}',
                "call-1",
                1.0,
            ),
            _tool_result("customer_support_specialist", "call-1", 2.0),
            {
                "agent_name": "collaboration_ops_specialist",
                "event": "delegate_error",
                "ts": 3.0,
                "data": {"error": "TimeoutError: timed out"},
            },
        ],
        failure_reason="batch#1 subtask#1 request failed: TimeoutError: timed out",
    )

    summary = await run_pilot(
        cases=("mcp_single_145",),
        baseline_dir=tmp_path,
        spec_dir=tmp_path,
        endpoint_config_path=None,
        schema_provider=_schema_provider,
    )

    case = summary["cases"][0]
    assert case["closure"]["decision"]["verdict"] == "block"
    assert "knowledge_base_specialist.update_knowledge" in case["closure"]["payload"]["missing_actions"]
    assert any(
        question.startswith("timeout_or_partial_trace:")
        for question in case["closure"]["payload"]["open_questions"]
    )


@pytest.mark.asyncio
async def test_run_pilot_summary_and_report_show_identity_mismatch(tmp_path: Path) -> None:
    _write_artifacts(
        tmp_path,
        case="mcp_single_146",
        passed=True,
        timeout=False,
        ground_truth=[
            {
                "mcp_server_name": "csm",
                "tool_name": "update_case",
                "agent": "customer_support_specialist",
                "arguments": {"case_id": "CS-1"},
            }
        ],
        events=[
            _tool_call(
                "customer_support_specialist",
                "mcp_csm_call_tool",
                "update_case",
                '{"case_id": "CS-2"}',
                "call-1",
                1.0,
            ),
            _tool_result("customer_support_specialist", "call-1", 2.0),
        ],
    )

    summary = await run_pilot(
        cases=("mcp_single_146",),
        baseline_dir=tmp_path,
        spec_dir=tmp_path,
        endpoint_config_path=None,
        schema_provider=_schema_provider,
    )

    payload = summary["cases"][0]["closure"]["payload"]
    assert payload["missing_actions"] == []
    assert payload["alignment_misaligned_actions"] == [
        "customer_support_specialist.update_case"
    ]
    assert payload["alignment_argument_diff_count"] == 1
    assert payload["alignment_identity_mismatch_count"] == 1
    assert payload["alignment_soft_identity_diff_count"] == 0
    assert payload["open_questions"] == [
        "argument_mismatch: customer_support_specialist.update_case case_id 'CS-1' != 'CS-2'"
    ]

    report = _markdown_report(summary)
    assert "misaligned_actions=['customer_support_specialist.update_case']" in report
    assert "argument_diffs=1; identity_mismatches=1; soft_identity_diffs=0" in report


@pytest.mark.asyncio
async def test_run_pilot_summary_and_report_show_soft_identity_without_open_question(
    tmp_path: Path,
) -> None:
    _write_artifacts(
        tmp_path,
        case="mcp_single_146",
        passed=True,
        timeout=False,
        ground_truth=[
            {
                "mcp_server_name": "teams",
                "tool_name": "send_chat_message",
                "agent": "collaboration_ops_specialist",
                "arguments": {"userId": "me", "content": "please review"},
            }
        ],
        events=[
            _tool_call(
                "collaboration_ops_specialist",
                "mcp_teams_call_tool",
                "send_chat_message",
                '{"userId": "alice@example.com", "content": "please review"}',
                "call-1",
                1.0,
            ),
            _tool_result("collaboration_ops_specialist", "call-1", 2.0),
        ],
    )

    summary = await run_pilot(
        cases=("mcp_single_146",),
        baseline_dir=tmp_path,
        spec_dir=tmp_path,
        endpoint_config_path=None,
        schema_provider=_schema_provider,
    )

    payload = summary["cases"][0]["closure"]["payload"]
    assert payload["alignment_misaligned_actions"] == []
    assert payload["alignment_argument_diff_count"] == 1
    assert payload["alignment_identity_mismatch_count"] == 0
    assert payload["alignment_soft_identity_diff_count"] == 1
    assert payload["open_questions"] == []

    report = _markdown_report(summary)
    assert "misaligned_actions=[]" in report
    assert "argument_diffs=1; identity_mismatches=0; soft_identity_diffs=1" in report


def _write_artifacts(
    root: Path,
    *,
    case: str,
    passed: bool,
    timeout: bool,
    ground_truth: list[dict],
    events: list[dict],
    failure_reason: str = "",
) -> None:
    spec = {
        "task_id": case,
        "description": case,
        "sub_task_list": [{"ground_truth": ground_truth}],
    }
    (root / f"{case}.json").write_text(json.dumps([spec]) + "\n", encoding="utf-8")
    (root / f"{case}_result.jsonl").write_text(
        json.dumps({"task_id": case, "task_passed": passed, "run_total_tokens": 100}) + "\n",
        encoding="utf-8",
    )
    (root / f"{case}_traj.jsonl").write_text(
        json.dumps(
            {
                "batch_entry": {
                    "task_results": [
                        {
                            "all_agent_traces": {
                                "collaboration_ops_specialist": {"trace": {"events": events}}
                            }
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "analysis_summary.json").write_text(
        json.dumps(
            [
                {
                    "case": case,
                    "task_id": case,
                    "description": case,
                    "timeout": timeout,
                    "task_passed": passed,
                    "failed_agents": [] if passed else ["knowledge_base_specialist"],
                    "failure_reason": failure_reason,
                    "errors": [failure_reason] if failure_reason else [],
                    "trace_event_counts": {"collaboration_ops_specialist": len(events)},
                }
            ]
        ),
        encoding="utf-8",
    )


def _tool_call(
    agent: str,
    wrapper_tool: str,
    tool_name: str,
    arguments_json: str,
    call_id: str,
    ts: float,
) -> dict:
    return {
        "agent_name": agent,
        "event": "tool_call",
        "ts": ts,
        "data": {
            "tool_call_id": call_id,
            "tool_name": wrapper_tool,
            "arguments": {
                "tool_name": tool_name,
                "arguments_json": arguments_json,
            },
        },
    }


def _tool_result(agent: str, call_id: str, ts: float) -> dict:
    return {
        "agent_name": agent,
        "event": "tool_result",
        "ts": ts,
        "data": {"tool_call_id": call_id, "status": "ok", "result": {"ok": True}},
    }


def _schema_provider(server: str, tool_name: str) -> dict:
    if server == "teams" and tool_name == "send_channel_message":
        return {
            "name": tool_name,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "teamId": {"type": "string"},
                    "body": {"type": ["object", "null"]},
                },
                "required": ["teamId", "body"],
            },
        }
    if server == "teams" and tool_name == "send_chat_message":
        return {
            "name": tool_name,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "userId": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["userId", "content"],
            },
        }
    return {
        "name": tool_name,
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        },
    }
