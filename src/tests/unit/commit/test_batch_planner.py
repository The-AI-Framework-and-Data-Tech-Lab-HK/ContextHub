"""Unit tests for token-aware commit micro-batch planner."""

from __future__ import annotations

from core.commit.batch_planner import plan_prepare_micro_batches
from core.commit.service import CommitCommand


def _command(i: int, *, payload_size: int = 200) -> CommitCommand:
    text = "x" * payload_size
    return CommitCommand(
        agent_id="agent-test",
        account_id="account-test",
        session_id=f"s-{i}",
        task_id=f"t-{i}",
        trajectory=[
            {"Step": 1, "Action": text, "Action_result": text, "meta": {"role": "AIMessage"}},
            {"Step": 2, "Action_result": text, "meta": {"role": "ToolMessage"}},
        ],
        labels={},
    )


def test_plan_prepare_micro_batches_respects_item_cap() -> None:
    commands = [_command(i, payload_size=50) for i in range(5)]
    batches, budget, max_ctx = plan_prepare_micro_batches(
        commands,
        llm_token_usage_ratio=0.9,
        max_items_per_batch=2,
        max_context_tokens_fallback=100000,
        model=None,
        api_key=None,
        base_url=None,
    )
    assert budget == 90000
    assert max_ctx == 100000
    assert [b.indices for b in batches] == [[0, 1], [2, 3], [4]]


def test_plan_prepare_micro_batches_splits_on_token_budget() -> None:
    commands = [_command(i, payload_size=5000) for i in range(3)]
    batches, budget, _ = plan_prepare_micro_batches(
        commands,
        llm_token_usage_ratio=0.5,
        max_items_per_batch=16,
        max_context_tokens_fallback=6000,
        model=None,
        api_key=None,
        base_url=None,
    )
    assert budget == 3000
    # With tiny budget and large items, each command should fall into its own micro-batch.
    assert [b.indices for b in batches] == [[0], [1], [2]]

