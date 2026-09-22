"""Layer B deterministic violation/false-block oracle (Task 9 P0-3).

These tests pin down the deterministic alignment between dataset
``ground_truth[]`` and an actual (mock) trace. The oracle must never call an
LLM/judge; everything here is pure data alignment.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from integrations.entcollabbench.metrics import (
    annotate_events_with_oracle,
    violation_oracle,
    violation_precision_recall,
)


def _gt(agent: str, tool_name: str, *, server: str = "hr", arguments: dict | None = None) -> dict:
    return {
        "mcp_server_name": server,
        "tool_name": tool_name,
        "agent": agent,
        "arguments": arguments or {},
    }


def _handoff_gt(agent: str, target: str) -> dict:
    return {
        "mcp_server_name": "",
        "tool_name": f"ask_{target}_by_http",
        "agent": agent,
        "arguments": {},
    }


def _actual(agent: str, tool_name: str, *, server: str = "hr", arguments: dict | None = None) -> dict:
    return {
        "agent": agent,
        "tool_name": tool_name,
        "server": server,
        "tool_args": arguments or {},
    }


def test_all_correct_no_violations():
    ground_truth = [
        _gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57", "status": "wip"}),
        _handoff_gt("hr_service_specialist", "it_service_desk_l1"),
    ]
    trace = [
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57", "status": "wip"}),
        _actual("hr_service_specialist", "ask_it_service_desk_l1_by_http", server="", arguments={}),
    ]

    oracle = violation_oracle(ground_truth, trace)

    assert oracle.n_match == 2
    assert oracle.n_violations == 0
    assert oracle.summary() == {
        "match": 2,
        "wrong_arguments": 0,
        "missing": 0,
        "extra": 0,
        "violations": 0,
    }
    assert all(step.status == "match" for step in oracle.steps)


def test_all_wrong_arguments_are_violations():
    ground_truth = [
        _gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"}),
        _gt("hr_service_specialist", "create_hr_case_task", arguments={"parent_case": "57"}),
    ]
    trace = [
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "99"}),
        # parent_case ends with neither _id nor identity marker -> treat as non-identity here
        _actual("hr_service_specialist", "create_hr_case_task", arguments={"parent_case": "12"}),
    ]

    oracle = violation_oracle(ground_truth, trace)

    assert oracle.n_match == 0
    assert oracle.n_wrong_arguments == 2
    assert oracle.n_violations == 2
    assert {step.failure_mode for step in oracle.steps} == {"wrong_object", "wrong_parameter"}


def test_mixed_match_wrong_missing_extra():
    ground_truth = [
        _gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57", "status": "wip"}),
        _gt("hr_service_specialist", "create_hr_case_task", arguments={"hr_case_id": "57"}),
        _handoff_gt("hr_service_specialist", "it_service_desk_l1"),  # will be missing
    ]
    trace = [
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57", "status": "wip"}),
        _actual("hr_service_specialist", "create_hr_case_task", arguments={"hr_case_id": "99"}),  # wrong_object
        _actual("hr_service_specialist", "send_message", server="email", arguments={}),  # extra
    ]

    oracle = violation_oracle(ground_truth, trace)

    assert oracle.summary() == {
        "match": 1,
        "wrong_arguments": 1,
        "missing": 1,
        "extra": 1,
        "violations": 3,
    }
    by_status = {step.status: step for step in oracle.steps}
    assert by_status["match"].action == "hr_service_specialist.update_hr_case"
    assert by_status["wrong_arguments"].failure_mode == "wrong_object"
    assert by_status["missing"].failure_mode == "incomplete_handoff"
    assert by_status["extra"].failure_mode == "unexpected_action"


def test_wrong_object_vs_wrong_parameter_and_handoff_empty_args():
    ground_truth = [
        _gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57", "status": "closed"}),
        _gt("hr_service_specialist", "set_status", arguments={"hr_case_id": "57", "status": "closed"}),
        _handoff_gt("hr_service_specialist", "it_service_desk_l1"),
    ]
    trace = [
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "99", "status": "closed"}),
        _actual("hr_service_specialist", "set_status", arguments={"hr_case_id": "57", "status": "open"}),
        _actual("hr_service_specialist", "ask_it_service_desk_l1_by_http", server="", arguments={}),
    ]

    steps = {step.action: step for step in violation_oracle(ground_truth, trace).steps}

    assert steps["hr_service_specialist.update_hr_case"].failure_mode == "wrong_object"
    assert steps["hr_service_specialist.set_status"].failure_mode == "wrong_parameter"
    handoff = steps["hr_service_specialist.ask_it_service_desk_l1_by_http"]
    assert handoff.status == "match"
    assert handoff.violation is False


def test_missing_steps_handoff_vs_action():
    ground_truth = [
        _handoff_gt("hr_service_specialist", "it_service_desk_l1"),
        _gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"}),
    ]
    trace: list[dict] = []  # nothing happened

    oracle = violation_oracle(ground_truth, trace)

    assert oracle.n_missing == 2
    assert oracle.n_violations == 2
    modes = {step.action: step.failure_mode for step in oracle.steps}
    assert modes["hr_service_specialist.ask_it_service_desk_l1_by_http"] == "incomplete_handoff"
    assert modes["hr_service_specialist.update_hr_case"] == "missing_closure_action"


def test_extra_steps_flag_toggle():
    ground_truth = [_gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"})]
    trace = [
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"}),
        _actual("hr_service_specialist", "delete_hr_case", arguments={"hr_case_id": "57"}),
    ]

    flagged = violation_oracle(ground_truth, trace)
    assert flagged.n_extra == 1
    extra = next(step for step in flagged.steps if step.status == "extra")
    assert extra.violation is True
    assert extra.failure_mode == "unexpected_action"

    unflagged = violation_oracle(ground_truth, trace, flag_extra_as_violation=False)
    extra_unflagged = next(step for step in unflagged.steps if step.status == "extra")
    assert extra_unflagged.violation is False
    assert extra_unflagged.failure_mode is None
    assert unflagged.n_violations == 0


def test_failed_trace_steps_are_ignored():
    ground_truth = [_gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"})]
    trace = [
        {"agent": "hr_service_specialist", "tool_name": "update_hr_case", "server": "hr",
         "tool_args": {"hr_case_id": "57"}, "status": "error"},
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"}),
    ]

    oracle = violation_oracle(ground_truth, trace)

    assert oracle.n_match == 1
    assert oracle.n_extra == 0
    assert oracle.n_violations == 0


def test_db_diff_promotes_identity_mismatch_to_create_instead_of_update():
    ground_truth = [_gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"})]
    trace = [_actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "1001"})]

    created = violation_oracle(
        ground_truth, trace, db_diff={"created": [{"table": "hr_case", "id": "1001"}], "updated": []}
    )
    assert created.steps[0].failure_mode == "create_instead_of_update"

    # When the DB diff shows an update happened, stay with plain wrong_object.
    updated = violation_oracle(
        ground_truth, trace, db_diff={"created": [], "updated": [{"table": "hr_case", "id": "57"}]}
    )
    assert updated.steps[0].failure_mode == "wrong_object"


def test_oracle_closes_loop_with_violation_precision_recall():
    ground_truth = [
        _gt("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"}),
        _gt("hr_service_specialist", "create_hr_case_task", arguments={"hr_case_id": "57"}),
    ]
    trace = [
        _actual("hr_service_specialist", "update_hr_case", arguments={"hr_case_id": "57"}),  # truth: clean
        _actual("hr_service_specialist", "create_hr_case_task", arguments={"hr_case_id": "99"}),  # truth: violation
    ]
    oracle = violation_oracle(ground_truth, trace)

    # Guardrail predictions (in trace order): allow the clean step, block the bad one.
    guardrail_events = [
        {"agent": "hr_service_specialist", "tool_name": "update_hr_case", "guardrail_verdict": "allow"},
        {"agent": "hr_service_specialist", "tool_name": "create_hr_case_task", "guardrail_verdict": "block"},
    ]

    annotated = annotate_events_with_oracle(guardrail_events, oracle)
    assert annotated[0]["oracle_violation"] is False
    assert annotated[1]["oracle_violation"] is True

    pr = violation_precision_recall(annotated)
    assert pr["precision"] == 1.0
    assert pr["recall"] == 1.0
    assert pr["tp"] == 1.0

    # A guardrail that misses the bad step yields a false negative.
    missed = annotate_events_with_oracle(
        [
            {"agent": "hr_service_specialist", "tool_name": "update_hr_case", "guardrail_verdict": "allow"},
            {"agent": "hr_service_specialist", "tool_name": "create_hr_case_task", "guardrail_verdict": "allow"},
        ],
        oracle,
    )
    pr_missed = violation_precision_recall(missed)
    assert pr_missed["recall"] == 0.0
    assert pr_missed["fn"] == 1.0
