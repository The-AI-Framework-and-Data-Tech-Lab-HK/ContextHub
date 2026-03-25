from __future__ import annotations

from core.commit.summarizer import summarize_trajectory


def test_llm_summarizer_result_is_preferred() -> None:
    steps = [
        {"Step": 1, "meta": {"role": "AIMessage"}, "Action": "local_db_sql(command='select 1')"},
        {"Step": 2, "meta": {"role": "ToolMessage"}, "Action_result": "{'status': 'success'}"},
    ]

    def _fake_llm(_steps):
        return ("L0 from llm", "L1 from llm")

    l0, l1 = summarize_trajectory(steps, llm_summarizer=_fake_llm)
    assert l0 == "L0 from llm"
    assert l1 == "L1 from llm"


def test_llm_failure_falls_back_to_rule_based() -> None:
    steps = [
        {"Step": 1, "meta": {"role": "AIMessage"}, "Action": "local_db_sql(command='select 1')"},
        {"Step": 2, "meta": {"role": "ToolMessage"}, "Action_result": "{'status': 'success'}"},
    ]

    def _bad_llm(_steps):
        raise RuntimeError("boom")

    l0, l1 = summarize_trajectory(steps, llm_summarizer=_bad_llm)
    assert "Trajectory with" in l0
    assert "Steps=" in l1

