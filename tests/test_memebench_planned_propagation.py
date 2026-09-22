from __future__ import annotations

import uuid

import pytest

from integrations.memebench.planned_propagation import (
    CONTINUATION_MODE,
    PlannedDerivedMemoryRule,
    PlannedEdgeMode,
    RecomputeResult,
    execute_frontier,
    graph_problem_from_published_edges,
    plan_published_graph,
    published_edge_pairs,
)


class CountingChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []
        self.total_tokens = 0

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        self.total_tokens += max(1, len(prompt) // 4) + 3
        return self.reply


class FakeRepo:
    def __init__(self, texts: dict) -> None:
        self._texts = {str(key): value for key, value in texts.items()}

    def session(self, account_id: str):
        texts = self._texts

        class _Ctx:
            async def __aenter__(self_):
                class _Db:
                    async def fetchrow(self__, sql, ctx_id):
                        text = texts.get(str(ctx_id), "derived note about the team lead")
                        return {
                            "l2_content": text,
                            "l1_content": None,
                            "l0_content": None,
                        }

                return _Db()

            async def __aexit__(self_, *exc):
                return False

        return _Ctx()


CONTRACT = {
    "J1": {"expected_cost": 0.0, "delta": 0.4},
    "J3": {"expected_cost": 20.0, "delta": 0.2},
    "J4": {"expected_cost": 80.0, "delta": 0.05},
    "cascade": {"expected_cost": 30.0, "delta": 0.05},
    "direct-stale": {"expected_cost": 1000.0, "delta": 0.0},
}


def _ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _event(node, account="acct"):
    return {
        "change_type": "modified",
        "context_id": node,
        "account_id": account,
        "diff_summary": "The team lead changed from Alice to Bob.",
        "metadata": {"before": "Alice", "after": "Bob"},
    }


def _rule(modes, cheap="YES\ncheap", strong="YES\nstrong", texts=None):
    cheap_chat = CountingChat(cheap)
    strong_chat = CountingChat(strong)
    rule = PlannedDerivedMemoryRule(
        modes=modes,
        repo=FakeRepo(texts or {}),
        strong_chat=strong_chat,
        cheap_chat=cheap_chat,
    )
    return rule, cheap_chat, strong_chat


def _changed(old_version: int) -> RecomputeResult:
    return RecomputeResult(
        changed=True,
        old_version=old_version,
        new_version=old_version + 1,
        old_semantic_identity=f"old-{old_version}",
        new_semantic_identity=f"new-{old_version + 1}",
    )


@pytest.mark.asyncio
async def test_j1_uses_frozen_rule_without_llm() -> None:
    root, child, _ = _ids()
    rule, cheap, strong = _rule(
        [PlannedEdgeMode(root, child, "J1")],
        texts={child: "The report owner is Carol assigned by the team lead."},
    )
    action = await rule.evaluate(_event(root), {"dependent_id": child})
    assert action.action == "mark_stale"
    assert cheap.calls == 0 and strong.calls == 0


@pytest.mark.asyncio
async def test_j3_calls_only_cheap() -> None:
    root, child, _ = _ids()
    rule, cheap, strong = _rule([PlannedEdgeMode(root, child, "J3")])
    action = await rule.evaluate(_event(root), {"dependent_id": child})
    assert action.action == "mark_stale"
    assert cheap.calls == 1 and strong.calls == 0


@pytest.mark.asyncio
async def test_j4_calls_only_strong() -> None:
    root, child, _ = _ids()
    rule, cheap, strong = _rule([PlannedEdgeMode(root, child, "J4")])
    await rule.evaluate(_event(root), {"dependent_id": child})
    assert cheap.calls == 0 and strong.calls == 1


@pytest.mark.asyncio
async def test_cascade_cheap_stale_short_circuits() -> None:
    root, child, _ = _ids()
    rule, cheap, strong = _rule(
        [PlannedEdgeMode(root, child, "cascade")],
        cheap="YES\ncheap stale",
        strong="NO\nshould not run",
    )
    action = await rule.evaluate(_event(root), {"dependent_id": child})
    assert action.action == "mark_stale"
    assert cheap.calls == 1 and strong.calls == 0


@pytest.mark.asyncio
async def test_cascade_cheap_fresh_escalates() -> None:
    root, child, _ = _ids()
    rule, cheap, strong = _rule(
        [PlannedEdgeMode(root, child, "cascade")],
        cheap="NO\ncheap fresh",
        strong="YES\nstrong stale",
    )
    action = await rule.evaluate(_event(root), {"dependent_id": child})
    assert action.action == "mark_stale"
    assert cheap.calls == 1 and strong.calls == 1


@pytest.mark.asyncio
async def test_direct_stale_does_not_call_llm() -> None:
    root, child, _ = _ids()
    rule, cheap, strong = _rule([PlannedEdgeMode(root, child, "direct-stale")])
    action = await rule.evaluate(_event(root), {"dependent_id": child})
    assert action.action == "mark_stale"
    assert cheap.calls == 0 and strong.calls == 0


def test_direct_stale_rejects_uncontracted_semantic_recompute() -> None:
    root, child, _ = _ids()
    with pytest.raises(ValueError, match="output-validity contract"):
        PlannedDerivedMemoryRule(
            modes=[PlannedEdgeMode(root, child, "direct-stale")],
            repo=FakeRepo({}),
            strong_chat=CountingChat("unused"),
            cheap_chat=CountingChat("unused"),
            recompute_on_stale=True,
        )


@pytest.mark.asyncio
async def test_recompute_change_continues_receding_horizon_a_b_c() -> None:
    root, middle, leaf = _ids()
    rule, _, _ = _rule(
        [
            PlannedEdgeMode(root, middle, "direct-stale"),
            PlannedEdgeMode(middle, leaf, "direct-stale"),
        ]
    )
    calls = []

    async def recompute(node, source_version, event):
        calls.append((node, source_version, event["event_id"]))
        return _changed(source_version)

    run = await execute_frontier(
        rule,
        root_id=root,
        adjacency={root: [middle], middle: [leaf]},
        account_id="acct",
        recompute=recompute,
    )
    assert [(src, dst) for src, dst, _ in run.executed_edges] == [
        (root, middle),
        (middle, leaf),
    ]
    assert [node for node, _, _ in calls] == [middle, leaf]
    assert run.receding_horizon_complete
    assert any(item["type"] == "recompute_result" for item in run.trace)


@pytest.mark.asyncio
async def test_semantically_unchanged_middle_stops_before_c() -> None:
    root, middle, leaf = _ids()
    rule, _, _ = _rule(
        [
            PlannedEdgeMode(root, middle, "direct-stale"),
            PlannedEdgeMode(middle, leaf, "direct-stale"),
        ]
    )

    async def unchanged(node, source_version, event):
        return RecomputeResult(
            changed=False,
            old_version=7,
            new_version=7,
            old_semantic_identity="same",
            new_semantic_identity="same",
        )

    run = await execute_frontier(
        rule,
        root_id=root,
        adjacency={root: [middle], middle: [leaf]},
        account_id="acct",
        recompute=unchanged,
    )
    assert [(src, dst) for src, dst, _ in run.executed_edges] == [(root, middle)]
    assert run.receding_horizon_complete


@pytest.mark.asyncio
async def test_cycle_or_event_limit_is_explicitly_unfinished() -> None:
    root, middle, _ = _ids()
    rule, _, _ = _rule(
        [
            PlannedEdgeMode(root, middle, "direct-stale"),
            PlannedEdgeMode(middle, root, "direct-stale"),
        ]
    )

    async def always_changes(node, source_version, event):
        return _changed(source_version)

    run = await execute_frontier(
        rule,
        root_id=root,
        adjacency={root: [middle], middle: [root]},
        account_id="acct",
        recompute=always_changes,
        max_events=3,
    )
    assert run.unfinished
    assert run.receding_horizon_complete is False
    assert any("limit exceeded" in error for error in run.errors)
    assert rule.token_ledger.realized_cheap == 0
    assert rule.token_ledger.realized_strong == 0


@pytest.mark.asyncio
async def test_missing_plan_fails_closed_to_direct_stale() -> None:
    root, child, extra = _ids()
    rule, cheap, strong = _rule([PlannedEdgeMode(root, extra, "J3")])
    action = await rule.evaluate(_event(root), {"dependent_id": child})
    assert action.action == "mark_stale"
    assert "planning_error" in action.reason
    assert cheap.calls == 0 and strong.calls == 0
    assert rule.planning_errors
    assert rule.executed[-1][2] == "direct-stale"


@pytest.mark.asyncio
async def test_frontier_only_and_unexecuted_edges_have_zero_realized_tokens() -> None:
    root, mid, leaf = _ids()
    modes = [
        PlannedEdgeMode(root, mid, "J3"),
        PlannedEdgeMode(mid, leaf, "J4"),
    ]
    cheap = CountingChat("NO\nkeep fresh")
    strong = CountingChat("YES\nshould not run on unreached hop")
    rule = PlannedDerivedMemoryRule(
        modes=modes,
        repo=FakeRepo({}),
        strong_chat=strong,
        cheap_chat=cheap,
    )
    run = await execute_frontier(
        rule,
        root_id=root,
        adjacency={root: [mid], mid: [leaf]},
        account_id="acct",
    )
    executed = {(src, dst) for src, dst, _ in run.executed_edges}
    assert (root, mid) in executed
    assert (mid, leaf) not in executed
    assert strong.calls == 0
    assert run.realized_tokens["strong_tokens"] == 0
    assert run.receding_horizon_complete is False
    assert run.continuation_mode == CONTINUATION_MODE


def test_evaluation_gold_does_not_enter_plan() -> None:
    a, b, c = _ids()
    rows = [
        {
            "dependency_id": a,
            "dependent_id": b,
            "should_stale": True,
            "gold_edge": True,
        },
        {
            "dependency_id": b,
            "dependent_id": c,
            "should_stale": False,
            "gold_edge": False,
        },
    ]
    pairs = published_edge_pairs(rows)
    assert len(pairs) == 2
    problem = graph_problem_from_published_edges(pairs, contract=CONTRACT, epsilon=0.5)
    assert len(problem.edges) == 2
    for edge in problem.edges:
        assert not hasattr(edge, "should_stale")
    planned = plan_published_graph(
        rows, contract=CONTRACT, epsilon=0.5, contract_method="point"
    )
    assert planned["n_edges"] == 2
    assert planned["diagnostic_only"] is True
    assert planned["certified"] is False
    assert planned["receding_horizon_complete"] is False


def test_cp_upper_plan_is_not_point_only() -> None:
    a, b, _ = _ids()
    planned = plan_published_graph(
        [(a, b)],
        contract=CONTRACT,
        epsilon=0.1,
        contract_method="cp-upper",
    )
    assert planned["certified"] is False
    assert (
        "receding_horizon_recompute_incomplete"
        in planned["certification_blocked_reason"]
    )
    assert planned["diagnostic_only"] is False
    assert planned["continuation_mode"] == CONTINUATION_MODE
    mismatched = plan_published_graph(
        [(a, b)],
        contract=CONTRACT,
        epsilon=0.1,
        contract_method="cp-upper",
        contract_distribution_mismatch=True,
    )
    assert mismatched["certified"] is False
    assert "contract_distribution_mismatch" in mismatched["certification_blocked_reason"]
    leaked = plan_published_graph(
        [(a, b)],
        contract=CONTRACT,
        epsilon=0.1,
        contract_method="cp-upper",
        evaluation_episodes_in_calibration=True,
    )
    assert leaked["certified"] is False
    assert "evaluation_episodes_in_calibration" in leaked["certification_blocked_reason"]


def test_false_receding_horizon_can_never_certify() -> None:
    a, b, _ = _ids()
    planned = plan_published_graph(
        [(a, b)],
        contract=CONTRACT,
        epsilon=0.1,
        contract_method="cp-upper",
    )
    assert planned["receding_horizon_complete"] is False
    assert planned["certified"] is False
    assert "marked_stale_proxy" in planned["certification_blocked_reason"]




def test_disjoint_chains_use_tree_solver_not_chain() -> None:
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    planned = plan_published_graph(
        [(a, b), (c, d)],
        contract=CONTRACT,
        epsilon=0.5,
        contract_method="cp-upper",
    )
    assert planned["n_sources"] == 2
    assert planned["n_targets"] == 2
    assert planned["topology"] == "tree"
    assert planned["solver"] != "chain"


def test_solver_exception_fallback_is_never_certified(monkeypatch) -> None:
    a, b, c, d = (uuid.uuid4() for _ in range(4))

    def explode(_problem):
        raise RuntimeError("solver crashed")

    monkeypatch.setattr(
        "integrations.memebench.planned_propagation.dag_milp_plan", explode
    )
    planned = plan_published_graph(
        [(a, b), (a, c), (b, d), (c, d)],
        contract=CONTRACT,
        epsilon=0.5,
        contract_method="cp-upper",
    )
    assert planned["solver"] == "heuristic"
    assert planned["planner_fell_back"] is True
    assert planned["certified"] is False
    assert "planner_fell_back" in planned["certification_blocked_reason"]


