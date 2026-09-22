from __future__ import annotations

import json

from integrations.entcollabbench.decision_log import (
    load_records,
    summarize,
    summarize_file,
)


def _tool_record(**overrides) -> dict:
    base = {
        "ts": 1.0,
        "agent_id": "collaboration_ops_specialist",
        "server": "teams",
        "tool_name": "send_channel_message",
        "mode": "observe",
        "verdict": "allow",
        "action": "allow",
        "allow": True,
        "would_block": False,
        "would_repair": False,
        "forwarded": True,
        "patched": False,
        "schema_source": "injected-schema-provider",
        "reason": "ok",
    }
    base.update(overrides)
    return base


def test_summarize_counts_verdicts_and_rates() -> None:
    records = [
        _tool_record(),
        _tool_record(verdict="block", action="block", allow=False, would_block=True, forwarded=True),
        _tool_record(verdict="repair", action="retry_with_patch", would_repair=True, patched=True),
    ]

    summary = summarize(records)

    assert summary["total"] == 3
    assert summary["would_block"] == 1
    assert summary["would_repair"] == 1
    assert summary["patched"] == 1
    assert summary["by_verdict"] == {"allow": 1, "block": 1, "repair": 1}
    assert summary["would_block_rate"] == round(1 / 3, 6)
    assert summary["would_repair_rate"] == round(1 / 3, 6)


def test_summarize_action_fallback_without_flags() -> None:
    # Flags absent: classification falls back to the action field.
    records = [
        {"agent_id": "a", "tool_name": "t", "verdict": "block", "action": "pending"},
        {"agent_id": "a", "tool_name": "t", "verdict": "repair", "action": "retry_with_feedback"},
    ]

    summary = summarize(records)

    assert summary["would_block"] == 2  # pending + retry_with_feedback both count
    assert summary["would_repair"] == 0


def test_per_target_breakdown_groups_by_agent_and_target() -> None:
    records = [
        _tool_record(agent_id="hr_service_specialist", tool_name="update_hr_case"),
        _tool_record(
            agent_id="hr_service_specialist",
            tool_name="update_hr_case",
            verdict="block",
            action="block",
            would_block=True,
        ),
        _tool_record(agent_id="it_service_desk_l1", tool_name="resolve_incident"),
    ]

    summary = summarize(records)
    per_target = summary["per_target"]

    assert per_target["hr_service_specialist.update_hr_case"]["count"] == 2
    assert per_target["hr_service_specialist.update_hr_case"]["would_block"] == 1
    assert per_target["it_service_desk_l1.resolve_incident"]["count"] == 1


def test_handoff_records_label_by_recipient() -> None:
    records = [
        {
            "agent_id": "hr_service_specialist",
            "recipient": "it_service_desk_l1",
            "verdict": "repair",
            "action": "retry_with_patch",
            "would_repair": True,
        }
    ]

    summary = summarize(records)

    assert "hr_service_specialist.it_service_desk_l1" in summary["per_target"]
    assert summary["would_repair"] == 1


def test_error_records_counted() -> None:
    records = [_tool_record(verdict="error", action="error", error="boom", forwarded=True)]

    summary = summarize(records)

    assert summary["errors"] == 1


def test_load_and_summarize_file_roundtrip(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    rows = [_tool_record(), _tool_record(verdict="block", action="block", would_block=True)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    assert len(load_records(path)) == 2
    summary = summarize_file(path)
    assert summary["total"] == 2
    assert summary["would_block"] == 1
    assert summary["source"] == str(path)
