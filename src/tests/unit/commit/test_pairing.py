"""U-02: AIMessage / ToolMessage pairing (AMC_plan/13)."""

from __future__ import annotations

from typing import Any

import pytest

from core.commit.pairing import pair_ai_tool_steps

pytestmark = pytest.mark.unit


def test_traj1_traj5_alternating_pairs(sample_traj_dir: Any) -> None:
    import json

    for name in ("traj1.json", "traj5.json"):
        path = sample_traj_dir / name
        if not path.exists():
            pytest.skip(f"missing {path}")
        steps = json.loads(path.read_text(encoding="utf-8"))
        pairs = pair_ai_tool_steps(steps)
        assert pairs
        for p in pairs:
            assert p.ai_step >= 1
            if not p.pending_output:
                assert p.tool_step is not None
                assert p.tool_step > p.ai_step


def test_pending_when_fewer_tools_than_ai_messages() -> None:
    """More AIMessage rows than ToolMessage rows -> last pairs are pending."""
    steps: list[dict[str, Any]] = [
        {"Step": 1, "Thinking": "", "Action": 'local_db_sql(file_path="a.db", command="SELECT 1")', "Action_result": "", "meta": {"role": "AIMessage"}},
        {"Step": 2, "Thinking": "", "Action": 'local_db_sql(file_path="a.db", command="SELECT 2")', "Action_result": "", "meta": {"role": "AIMessage"}},
        {"Step": 3, "Thinking": "", "Action": "", "Action_result": "{'status': 'success'}", "meta": {"role": "ToolMessage"}},
    ]
    pairs = pair_ai_tool_steps(steps)
    assert len(pairs) == 2
    assert pairs[0].pending_output is False
    assert pairs[1].pending_output is True
    assert "pending_output" in pairs[1].quality_flags


def test_traj1_step14_write_report_gets_step15_tool_output(sample_traj_dir: Any) -> None:
    import json

    steps = json.loads((sample_traj_dir / "traj1.json").read_text(encoding="utf-8"))
    pairs = pair_ai_tool_steps(steps)
    p13 = next(p for p in pairs if p.ai_step == 13)
    p14 = next(p for p in pairs if p.ai_step == 14)
    assert p13.pending_output is True
    assert p13.tool_step is None
    assert p14.pending_output is False
    assert p14.tool_step == 15


def test_batched_ai_then_tools_keeps_fifo_when_compatibility_ties() -> None:
    steps: list[dict[str, Any]] = [
        {"Step": 1, "Thinking": "", "Action": 'local_db_sql(file_path="a.db", command="SELECT 1")', "Action_result": "", "meta": {"role": "AIMessage"}},
        {"Step": 2, "Thinking": "", "Action": 'local_db_sql(file_path="a.db", command="SELECT 2")', "Action_result": "", "meta": {"role": "AIMessage"}},
        {"Step": 3, "Thinking": "", "Action": 'local_db_sql(file_path="a.db", command="SELECT 3")', "Action_result": "", "meta": {"role": "AIMessage"}},
        {"Step": 4, "Thinking": "", "Action": "", "Action_result": "{'status': 'success', 'rows': 1}", "meta": {"role": "ToolMessage"}},
        {"Step": 5, "Thinking": "", "Action": "", "Action_result": "{'status': 'success', 'rows': 2}", "meta": {"role": "ToolMessage"}},
        {"Step": 6, "Thinking": "", "Action": "", "Action_result": "{'status': 'success', 'rows': 3}", "meta": {"role": "ToolMessage"}},
    ]
    pairs = pair_ai_tool_steps(steps)
    assert [p.ai_step for p in pairs] == [1, 2, 3]
    assert [p.tool_step for p in pairs] == [4, 5, 6]
