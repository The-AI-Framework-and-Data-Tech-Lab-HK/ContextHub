from __future__ import annotations

import json

import pytest

from contexthub.enforcement import Boundary, EnforcementContext, Verdict
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.models.request import RequestContext
from integrations.entcollabbench.closure_adapter import (
    build_approval_closure_payload,
    build_workflow_closure_payload,
    extract_completed_actions,
    ground_truth_steps,
    required_actions_from_ground_truth,
)


class FakeStaleness:
    async def any_stale_or_blocked_refs(self, db, refs):
        return []


def _tool_call(
    agent: str,
    tool_name: str,
    call_id: str,
    ts: float = 1.0,
    *,
    server: str = "csm",
    arguments: dict | None = None,
) -> dict:
    return {
        "agent_name": agent,
        "event": "tool_call",
        "ts": ts,
        "data": {
            "tool_call_id": call_id,
            "tool_name": f"mcp_{server}_call_tool",
            "arguments": {
                "tool_name": tool_name,
                "arguments_json": json.dumps(arguments or {}),
            },
        },
    }


def _tool_result(
    agent: str,
    call_id: str,
    ts: float = 2.0,
    *,
    status: str = "ok",
    error: str | None = None,
) -> dict:
    data = {
        "tool_call_id": call_id,
        "status": status,
        "result": {"id": "1"},
    }
    if error:
        data["error"] = error
    return {
        "agent_name": agent,
        "event": "tool_result",
        "ts": ts,
        "data": data,
    }


def _ground_truth() -> list[dict]:
    return [
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
    ]


def test_required_and_completed_actions_are_adapter_labels() -> None:
    gt = _ground_truth()
    events = [
        _tool_call(
            "customer_support_specialist",
            "update_case",
            "call-1",
            1.0,
            arguments={"case_id": "CS-1"},
        ),
        _tool_result("customer_support_specialist", "call-1", 2.0),
        _tool_call(
            "knowledge_base_specialist",
            "update_knowledge",
            "call-2",
            3.0,
            arguments={"knowledge_id": "KB-1"},
        ),
        _tool_result("knowledge_base_specialist", "call-2", 4.0),
    ]

    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_passed",
        ground_truth=gt,
        trace_events=events,
        runtime_summary={"timeout": False},
    )

    assert required_actions_from_ground_truth(gt) == [
        "customer_support_specialist.update_case",
        "knowledge_base_specialist.update_knowledge",
    ]
    assert payload["completed_actions"] == payload["anchor"]["required_actions"]
    assert payload["diagnostics"]["missing_actions"] == []
    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == []
    assert payload["open_questions"] == []


def test_fixed_spec_task_list_builds_required_actions_and_missing_update_knowledge() -> None:
    fixed_spec = [
        {
            "task_id": "mcp_single_145",
            "sub_task_list": [
                {
                    "ground_truth": [
                        {
                            "mcp_server_name": "",
                            "tool_name": "",
                            "agent": "collaboration_ops_specialist",
                            "arguments": {},
                        },
                        {
                            "mcp_server_name": "csm",
                            "tool_name": "update_case",
                            "agent": "customer_support_specialist",
                            "arguments": {"case_id": "CS-1"},
                        },
                        {
                            "mcp_server_name": "knowledge_base",
                            "tool_name": "update_knowledge",
                            "agent": "knowledge_base_specialist",
                            "arguments": {"knowledge_id": "KB-1"},
                        },
                    ]
                }
            ],
        }
    ]

    gt_steps = ground_truth_steps(fixed_spec)
    required_actions = required_actions_from_ground_truth(gt_steps)
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_145",
        ground_truth=fixed_spec,
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                1.0,
                arguments={"case_id": "CS-1"},
            ),
            _tool_result("customer_support_specialist", "call-1", 2.0),
        ],
        runtime_summary={"timeout": True, "failure_reason": "TimeoutError: timed out"},
    )

    assert required_actions == [
        "customer_support_specialist.update_case",
        "knowledge_base_specialist.update_knowledge",
    ]
    assert payload["anchor"]["required_actions"] == required_actions
    assert payload["diagnostics"]["missing_actions"] == [
        "knowledge_base_specialist.update_knowledge"
    ]
    assert any(
        "knowledge_base_specialist.update_knowledge" in question
        for question in payload["open_questions"]
    )


def test_failed_tool_result_is_not_completed() -> None:
    events = [
        _tool_call(
            "knowledge_base_specialist",
            "update_knowledge",
            "call-1",
            1.0,
            arguments={"knowledge_id": "KB-1"},
        ),
        _tool_result(
            "knowledge_base_specialist",
            "call-1",
            2.0,
            status="error",
            error="permission denied",
        ),
    ]

    completed, evidence, diagnostics = extract_completed_actions(events)

    assert completed == []
    assert evidence == {}
    assert diagnostics["failed_tool_results"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_missing_update_knowledge_blocks_closure_guardrail() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_145",
        ground_truth=_ground_truth(),
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                1.0,
                arguments={"case_id": "CS-1"},
            ),
            _tool_result("customer_support_specialist", "call-1", 2.0),
        ],
        runtime_summary={"timeout": True, "failure_reason": "TimeoutError: timed out"},
    )
    ec = EnforcementContext(
        boundary=Boundary.CLOSURE,
        actor=RequestContext(account_id="acme", agent_id="collaboration_ops_specialist"),
        payload=payload,
        workflow_id="mcp_single_145",
    )

    decision = await ClosureGuardrail(FakeStaleness()).check(None, ec)

    assert "knowledge_base_specialist.update_knowledge" in payload["diagnostics"]["missing_actions"]
    assert any("knowledge_base_specialist.update_knowledge" in q for q in payload["open_questions"])
    assert decision.verdict == Verdict.BLOCK
    assert decision.violations[0].repair_hint["missing_actions"] == [
        "knowledge_base_specialist.update_knowledge"
    ]


def test_s0_passed_complete_trace_has_no_missing_actions() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_146",
        ground_truth=_ground_truth(),
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                1.0,
                arguments={"case_id": "CS-1"},
            ),
            _tool_result("customer_support_specialist", "call-1", 2.0),
            _tool_call(
                "knowledge_base_specialist",
                "update_knowledge",
                "call-2",
                3.0,
                arguments={"knowledge_id": "KB-1"},
            ),
            _tool_result("knowledge_base_specialist", "call-2", 4.0),
        ],
        runtime_summary={"status": "ok", "timeout": False},
    )

    assert payload["diagnostics"]["missing_actions"] == []
    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == []
    assert payload["diagnostics"]["runtime"]["timeout"] is False
    assert payload["open_questions"] == []


def test_timeout_case_marks_incomplete_closure_evidence() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_145",
        ground_truth=_ground_truth(),
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                1.0,
                arguments={"case_id": "CS-1"},
            ),
            _tool_result("customer_support_specialist", "call-1", 2.0),
            {
                "agent_name": "collaboration_ops_specialist",
                "event": "delegate_error",
                "ts": 3.0,
                "data": {"error": "TimeoutError: timed out"},
            },
        ],
        runtime_summary={"status": "error", "timeout": True},
    )

    assert payload["diagnostics"]["runtime"]["timeout"] is True
    assert payload["diagnostics"]["runtime"]["partial_trace"] is True
    assert "knowledge_base_specialist.update_knowledge" in payload["diagnostics"]["missing_actions"]
    assert any(question.startswith("timeout_or_partial_trace:") for question in payload["open_questions"])


def test_identity_argument_alignment_adds_evidence_without_open_questions() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="identity-aligned",
        ground_truth=[
            {
                "mcp_server_name": "csm",
                "tool_name": "update_case",
                "agent": "customer_support_specialist",
                "arguments": {"case_id": "CS-1"},
            }
        ],
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                arguments={"case_id": "CS-1"},
            ),
            _tool_result("customer_support_specialist", "call-1"),
        ],
    )

    assert payload["completed_actions"] == ["customer_support_specialist.update_case"]
    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == []
    assert payload["open_questions"] == []
    assert (
        payload["evidence"]["customer_support_specialist.update_case#object:case_id=CS-1"]
        .startswith("trace://entcollab/customer_support_specialist/update_case?")
    )


def test_identity_argument_mismatch_is_reported_as_open_question() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="identity-mismatch",
        ground_truth=[
            {
                "mcp_server_name": "csm",
                "tool_name": "update_case",
                "agent": "customer_support_specialist",
                "arguments": {"case_id": "CS-1"},
            }
        ],
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                arguments={"case_id": "CS-2"},
            ),
            _tool_result("customer_support_specialist", "call-1"),
        ],
    )

    assert payload["diagnostics"]["missing_actions"] == []
    assert payload["completed_actions"] == ["customer_support_specialist.update_case"]
    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == [
        "customer_support_specialist.update_case"
    ]
    assert "customer_support_specialist.update_case#object:case_id=CS-1" not in payload["evidence"]
    assert payload["open_questions"] == [
        "argument_mismatch: customer_support_specialist.update_case case_id 'CS-1' != 'CS-2'"
    ]


def test_non_identity_text_mismatch_stays_diagnostic_only() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="non-identity-diff",
        ground_truth=[
            {
                "mcp_server_name": "csm",
                "tool_name": "update_case",
                "agent": "customer_support_specialist",
                "arguments": {"case_id": "CS-1", "status": "resolved"},
            }
        ],
        trace_events=[
            _tool_call(
                "customer_support_specialist",
                "update_case",
                "call-1",
                arguments={"case_id": "CS-1", "status": "closed"},
            ),
            _tool_result("customer_support_specialist", "call-1"),
        ],
    )

    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == []
    assert payload["open_questions"] == []
    assert payload["diagnostics"]["alignment"]["argument_diffs"][0]["non_identity_diffs"] == [
        {
            "field": "status",
            "expected": "resolved",
            "actual": "closed",
            "kind": "non_identity_mismatch",
        }
    ]


def test_collaboration_user_id_me_email_alias_stays_diagnostic_only() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="soft-user-id-alias",
        ground_truth=[
            {
                "mcp_server_name": "teams",
                "tool_name": "send_chat_message",
                "agent": "collaboration_ops_specialist",
                "arguments": {"userId": "me", "content": "please review"},
            }
        ],
        trace_events=[
            _tool_call(
                "collaboration_ops_specialist",
                "send_chat_message",
                "call-1",
                server="teams",
                arguments={"userId": "alice@example.com", "content": "please review"},
            ),
            _tool_result("collaboration_ops_specialist", "call-1"),
        ],
    )

    alignment = payload["diagnostics"]["alignment"]
    assert alignment["misaligned_actions"] == []
    assert payload["open_questions"] == []
    assert alignment["argument_diffs"][0]["identity_mismatches"] == []
    assert alignment["argument_diffs"][0]["soft_identity_diffs"] == [
        {
            "field": "userId",
            "expected": "me",
            "actual": "alice@example.com",
            "kind": "soft_identity_mismatch",
        }
    ]
    assert alignment["argument_diffs"][0]["non_identity_diffs"] == [
        {
            "field": "userId",
            "expected": "me",
            "actual": "alice@example.com",
            "kind": "soft_identity_mismatch",
        }
    ]


def test_calendar_primary_alias_stays_diagnostic_only() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="soft-calendar-id-alias",
        ground_truth=[
            {
                "mcp_server_name": "calendar",
                "tool_name": "update_event",
                "agent": "calendar_ops_specialist",
                "arguments": {"calendarId": "primary", "event_id": "evt-1"},
            }
        ],
        trace_events=[
            _tool_call(
                "calendar_ops_specialist",
                "update_event",
                "call-1",
                server="calendar",
                arguments={"calendarId": "alice-primary", "event_id": "evt-1"},
            ),
            _tool_result("calendar_ops_specialist", "call-1"),
        ],
    )

    alignment = payload["diagnostics"]["alignment"]
    assert alignment["misaligned_actions"] == []
    assert payload["open_questions"] == []
    assert alignment["argument_diffs"][0]["identity_mismatches"] == []
    assert alignment["argument_diffs"][0]["soft_identity_diffs"] == [
        {
            "field": "calendarId",
            "expected": "primary",
            "actual": "alice-primary",
            "kind": "soft_identity_mismatch",
        }
    ]


def test_knowledge_id_mismatch_remains_blocking_argument_mismatch() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="knowledge-id-mismatch",
        ground_truth=[
            {
                "mcp_server_name": "knowledge_base",
                "tool_name": "update_knowledge",
                "agent": "knowledge_base_specialist",
                "arguments": {"knowledge_id": "KB-1"},
            }
        ],
        trace_events=[
            _tool_call(
                "knowledge_base_specialist",
                "update_knowledge",
                "call-1",
                server="knowledge_base",
                arguments={"knowledge_id": "KB-2"},
            ),
            _tool_result("knowledge_base_specialist", "call-1"),
        ],
    )

    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == [
        "knowledge_base_specialist.update_knowledge"
    ]
    assert payload["open_questions"] == [
        "argument_mismatch: knowledge_base_specialist.update_knowledge knowledge_id 'KB-1' != 'KB-2'"
    ]


def test_teams_body_content_text_mismatch_does_not_block_pass_control_style_call() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="teams-pass-control",
        ground_truth=[
            {
                "mcp_server_name": "teams",
                "tool_name": "send_channel_message",
                "agent": "collaboration_ops_specialist",
                "arguments": {
                    "teamId": "team_techcorp_001",
                    "channelId": "channel_shared_001",
                    "body": {"content": "please continue"},
                },
            }
        ],
        trace_events=[
            _tool_call(
                "collaboration_ops_specialist",
                "send_channel_message",
                "call-1",
                server="teams",
                arguments={
                    "teamId": "team_techcorp_001",
                    "channelId": "channel_shared_001",
                    "content": "done",
                },
            ),
            _tool_result("collaboration_ops_specialist", "call-1"),
        ],
    )

    assert payload["diagnostics"]["missing_actions"] == []
    assert payload["diagnostics"]["alignment"]["misaligned_actions"] == []
    assert payload["open_questions"] == []


def test_approval_subset_payload_requires_decision() -> None:
    payload = build_approval_closure_payload(
        workflow_id="approval-wf",
        completed_actions=["review_policy"],
        decision_label="approve",
        rule_citations=["finance/rule-1"],
    )

    assert payload["require_decision"] is True
    assert payload["decision_label"] == "approve"
    assert payload["rule_citations"] == ["finance/rule-1"]
