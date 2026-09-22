"""Benchmark-only P2 executor driven by a frozen graph-level plan.

Does not modify the production ``DerivedMemoryOracleRule`` or
``PropagationEngine`` outbox semantics. Missing assignments fail closed to
``direct-stale``. Unexecuted future hops are not charged realized tokens.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Sequence
from uuid import UUID

from contexthub.planning.models import (
    EdgeOption,
    GraphProblem,
    PlanResult,
    PlanningInfeasibleError,
    PolicyCandidate,
)
from contexthub.planning.planners import (
    chain_risk_grid_plan,
    dag_milp_plan,
    maximum_path_risk,
    repair_large_dag_plan,
    tree_risk_grid_plan,
)
from contexthub.propagation.base import PropagationAction, PropagationRule
from contexthub.propagation.derived_memory_rule import DerivedMemoryOracleRule
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.propagation.skill_dep_rule import SkillVersionDepRule
from contexthub.propagation.table_schema_rule import TableSchemaRule
from integrations.memebench.chronological_policy import EPSILON_PROP
from integrations.memebench.common import judge_j1
from integrations.memebench.propagation_planner_eval import _classify

EdgeModeName = Literal["J1", "J3", "J4", "cascade", "direct-stale"]
PLAN_MODES: tuple[EdgeModeName, ...] = ("J1", "J3", "J4", "cascade", "direct-stale")
CONTINUATION_MODE = "marked_stale proxy"


@dataclass(frozen=True)
class PlannedEdgeMode:
    dependency_id: UUID
    dependent_id: UUID
    mode: EdgeModeName


@dataclass
class TokenLedger:
    """Realized tokens for edges that actually ran on the current frontier."""

    by_edge: dict[tuple[UUID, UUID], dict[str, int]] = field(default_factory=dict)

    def add(
        self,
        key: tuple[UUID, UUID],
        *,
        cheap: int = 0,
        strong: int = 0,
        mode: str,
    ) -> None:
        current = self.by_edge.setdefault(
            key, {"cheap_tokens": 0, "strong_tokens": 0, "mode": mode}
        )
        current["cheap_tokens"] += cheap
        current["strong_tokens"] += strong
        current["mode"] = mode

    @property
    def realized_cheap(self) -> int:
        return sum(item["cheap_tokens"] for item in self.by_edge.values())

    @property
    def realized_strong(self) -> int:
        return sum(item["strong_tokens"] for item in self.by_edge.values())

    def as_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for item in self.by_edge.values():
            mix[str(item["mode"])] = mix.get(str(item["mode"]), 0) + 1
        return mix


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def published_edge_pairs(
    rows: Iterable[Mapping[str, Any]] | Iterable[tuple[Any, Any]],
) -> list[tuple[UUID, UUID]]:
    """Keep every published edge. ``should_stale`` / gold fields are ignored."""

    pairs: list[tuple[UUID, UUID]] = []
    seen: set[tuple[UUID, UUID]] = set()
    for row in rows:
        if isinstance(row, Mapping):
            upstream = row.get("dependency_id", row.get("upstream"))
            dependent = row.get("dependent_id", row.get("dependent"))
            if upstream is None or dependent is None:
                raise ValueError("published edge is missing dependency/dependent ids")
        else:
            upstream, dependent = row
        key = (_as_uuid(upstream), _as_uuid(dependent))
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def default_menu_from_contract(
    contract: Mapping[str, Mapping[str, Any]],
) -> tuple[PolicyCandidate, ...]:
    return tuple(
        PolicyCandidate(
            name=name,
            cost=float(contract[name]["expected_cost"]),
            delta=float(contract[name]["delta"]),
        )
        for name in PLAN_MODES
    )


def graph_problem_from_published_edges(
    edges: Sequence[tuple[UUID, UUID]] | Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Mapping[str, Any]],
    epsilon: float = EPSILON_PROP,
) -> GraphProblem:
    pairs = published_edge_pairs(edges)
    if not pairs:
        raise ValueError("cannot plan an empty published graph")
    classified = _classify([(str(src), str(dst)) for src, dst in pairs])
    roots, targets, _topology = classified
    menu = default_menu_from_contract(contract)
    nodes = tuple(dict.fromkeys(
        [*(src for src, _ in pairs), *(dst for _, dst in pairs)]
    ))
    options = tuple(
        EdgeOption(upstream=src, dependent=dst, candidates=menu)
        for src, dst in pairs
    )
    return GraphProblem(
        nodes=nodes,
        edges=options,
        sources=tuple(_as_uuid(node) for node in roots),
        targets=tuple(_as_uuid(node) for node in targets),
        epsilon=epsilon,
    )


def _topology_of(problem: GraphProblem) -> str:
    """Classify the protected graph. A forest of chains is a tree, not a chain.

    ``chain_risk_grid_plan`` requires exactly one source and one target. Two
    disjoint chains satisfy per-node degree bounds but must not use that solver.
    """

    indegree: dict[Any, int] = {node: 0 for node in problem.nodes}
    outdegree: dict[Any, int] = {node: 0 for node in problem.nodes}
    for edge in problem.edges:
        indegree[edge.dependent] += 1
        outdegree[edge.upstream] += 1
    single_chain = (
        len(problem.sources) == 1
        and len(problem.targets) == 1
        and all(indegree[n] <= 1 and outdegree[n] <= 1 for n in problem.nodes)
    )
    if single_chain:
        return "chain"
    if all(indegree[n] <= 1 for n in problem.nodes):
        return "tree"
    return "dag"


def certification_flags(
    *,
    contract_method: str,
    feasible: bool,
    planner_infeasible: bool,
    missing_assignment_count: int,
    contract_distribution_mismatch: bool,
    evaluation_episodes_in_calibration: bool,
    planner_fell_back: bool = False,
    durable_drain_complete: bool = False,
    no_leftover: bool = False,
    no_dead_letter: bool = False,
    receding_horizon_complete: bool = False,
    risk_ledger_complete: bool = False,
    continuation_mode: str = CONTINUATION_MODE,
    semantic_recompute_available: bool = False,
    semantic_recompute_exercised: bool = False,
    held_out_data_independent: bool = False,
    development_reevaluation: bool = True,
) -> dict[str, Any]:
    """``certified`` is never implied by the contract_method string alone."""

    blocked: list[str] = []
    if contract_method != "cp-upper":
        blocked.append("not_cp_upper")
    if evaluation_episodes_in_calibration:
        blocked.append("evaluation_episodes_in_calibration")
    if contract_distribution_mismatch:
        blocked.append("contract_distribution_mismatch")
    if not feasible:
        blocked.append("plan_infeasible")
    if planner_infeasible:
        blocked.append("planner_fell_back")
    if planner_fell_back and "planner_fell_back" not in blocked:
        blocked.append("planner_fell_back")
    if missing_assignment_count:
        blocked.append("missing_assignments")
    if not durable_drain_complete:
        blocked.append("durable_drain_incomplete")
    if not no_leftover:
        blocked.append("leftover_events")
    if not no_dead_letter:
        blocked.append("dead_letter_events")
    invalidation_blocked = list(blocked)
    if not risk_ledger_complete:
        invalidation_blocked.append("risk_ledger_incomplete")
    durable_invalidation_certified = not invalidation_blocked
    if not semantic_recompute_available:
        blocked.append("semantic_recompute_unavailable")
    if not semantic_recompute_exercised:
        blocked.append("semantic_recompute_not_exercised")
    if not receding_horizon_complete:
        blocked.append("receding_horizon_recompute_incomplete")
    if continuation_mode == CONTINUATION_MODE:
        blocked.append("marked_stale_proxy")
    if not risk_ledger_complete:
        blocked.append("risk_ledger_incomplete")
    semantic_recompute_certified = not blocked
    return {
        # Backward-compatible key now means the strongest end-to-end capability.
        "certified": semantic_recompute_certified,
        "certification_scope": "durable_invalidation_only",
        "durable_invalidation_certified": durable_invalidation_certified,
        "semantic_recompute_available": semantic_recompute_available,
        "semantic_recompute_exercised": semantic_recompute_exercised,
        "semantic_recompute_certified": semantic_recompute_certified,
        "receding_horizon_recompute_complete": receding_horizon_complete,
        "diagnostic_only": contract_method == "point",
        "held_out_certification": bool(
            semantic_recompute_certified
            and held_out_data_independent
            and not development_reevaluation
        ),
        "held_out_data_independent": held_out_data_independent,
        "development_reevaluation": development_reevaluation,
        "contract_distribution_mismatch": contract_distribution_mismatch,
        "evaluation_episodes_in_calibration": evaluation_episodes_in_calibration,
        "planner_infeasible": planner_infeasible,
        "planner_fell_back": planner_fell_back or planner_infeasible,
        "missing_assignment_count": missing_assignment_count,
        "certification_blocked_reason": blocked,
    }


def _direct_stale_result(problem: GraphProblem) -> PlanResult:
    direct = next(
        candidate
        for candidate in problem.edges[0].candidates
        if candidate.name == "direct-stale"
    )
    assignments = {edge.key: direct for edge in problem.edges}
    risk, path = maximum_path_risk(problem, assignments)
    feasible = risk <= problem.epsilon
    return PlanResult(
        assignments=assignments,
        total_cost=sum(candidate.cost for candidate in assignments.values()),
        max_path_risk=risk,
        feasible=feasible,
        method="fail-closed-direct-stale",
        violating_path=None if feasible else path,
    )


def _solve_published(problem: GraphProblem, topology: str) -> tuple[str, PlanResult, bool]:
    order = {
        "chain": ("chain", "tree", "milp"),
        "tree": ("tree", "milp"),
        "dag": ("milp",),
    }[topology]
    solvers = {
        "chain": chain_risk_grid_plan,
        "tree": tree_risk_grid_plan,
        "milp": dag_milp_plan,
    }
    last_error: Exception | None = None
    for name in order:
        try:
            if name == "milp":
                try:
                    return name, solvers[name](problem), False
                except (PlanningInfeasibleError, RuntimeError, ValueError) as exc:
                    last_error = exc
                    return "heuristic", repair_large_dag_plan(problem), True
            return name, solvers[name](problem), False
        except (PlanningInfeasibleError, ValueError, RuntimeError) as exc:
            last_error = exc
            continue
    _ = last_error
    return "fail-closed-direct-stale", _direct_stale_result(problem), True


def plan_published_graph(
    edges: Sequence[tuple[UUID, UUID]] | Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Mapping[str, Any]],
    epsilon: float = EPSILON_PROP,
    contract_method: str = "cp-upper",
    contract_distribution_mismatch: bool = False,
    evaluation_episodes_in_calibration: bool = False,
) -> dict[str, Any]:
    problem = graph_problem_from_published_edges(
        edges, contract=contract, epsilon=epsilon
    )
    topology = _topology_of(problem)
    solver, result, planner_infeasible = _solve_published(problem, topology)
    modes = assignments_to_modes(result)
    mix: dict[str, int] = {}
    for mode in modes:
        mix[mode.mode] = mix.get(mode.mode, 0) + 1
    flags = certification_flags(
        contract_method=contract_method,
        feasible=result.feasible,
        planner_infeasible=planner_infeasible,
        missing_assignment_count=0,
        contract_distribution_mismatch=contract_distribution_mismatch,
        evaluation_episodes_in_calibration=evaluation_episodes_in_calibration,
        planner_fell_back=planner_infeasible,
        durable_drain_complete=False,
        no_leftover=False,
        no_dead_letter=False,
        receding_horizon_complete=False,
        risk_ledger_complete=False,
        continuation_mode=CONTINUATION_MODE,
    )
    return {
        "topology": topology,
        "solver": solver,
        "feasible": result.feasible,
        "max_path_risk": result.max_path_risk,
        "total_cost": result.total_cost,
        "method": result.method,
        "contract_method": contract_method,
        "epsilon_prop": epsilon,
        "receding_horizon_complete": False,
        "continuation_mode": CONTINUATION_MODE,
        "planned_mode_mix": mix,
        "assignments": modes,
        "assignment_audit": [
            {
                "dependency_id": str(item.dependency_id),
                "dependent_id": str(item.dependent_id),
                "mode": item.mode,
            }
            for item in modes
        ],
        "n_edges": len(modes),
        "n_sources": len(problem.sources),
        "n_targets": len(problem.targets),
        **flags,
    }


def assignments_to_modes(result: PlanResult) -> list[PlannedEdgeMode]:
    modes: list[PlannedEdgeMode] = []
    for (upstream, dependent), candidate in result.assignments.items():
        mode = candidate.name
        if mode not in PLAN_MODES:
            raise ValueError(f"planner returned unknown mode {mode!r}")
        modes.append(
            PlannedEdgeMode(
                dependency_id=_as_uuid(upstream),
                dependent_id=_as_uuid(dependent),
                mode=mode,  # type: ignore[arg-type]
            )
        )
    return modes


class _UsageChat:
    """Count tokens from an inner client without changing its prompt semantics."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.call_records: list[dict[str, Any]] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.calls += 1
        before = getattr(self._inner, "total_tokens", None)
        retry_before = int(getattr(self._inner, "retry_attempts", 0))
        prompt_before = int(getattr(self._inner, "prompt_tokens", 0))
        completion_before = int(getattr(self._inner, "completion_tokens", 0))
        answer = await self._inner.complete(prompt, max_tokens=max_tokens)
        after = getattr(self._inner, "total_tokens", None)
        prompt_delta = max(
            0, int(getattr(self._inner, "prompt_tokens", 0)) - prompt_before
        )
        completion_delta = max(
            0,
            int(getattr(self._inner, "completion_tokens", 0))
            - completion_before,
        )
        tokens_are_real = bool(getattr(self._inner, "tokens_are_real", False))
        if isinstance(before, int) and isinstance(after, int):
            delta = max(0, after - before)
            if prompt_delta + completion_delta == 0:
                prompt_delta = delta
        else:
            prompt_delta = len(prompt) // 4
            completion_delta = len(answer or "") // 4
            tokens_are_real = False
        self.prompt_tokens += prompt_delta
        self.completion_tokens += completion_delta
        self.call_records.append(
            {
                "model": getattr(self._inner, "model", None),
                "prompt": prompt,
                "raw_output": answer,
                "max_tokens": max_tokens,
                "prompt_tokens": prompt_delta,
                "completion_tokens": completion_delta,
                "tokens_are_real": tokens_are_real,
                "retry_attempt": max(
                    0, int(getattr(self._inner, "retry_attempts", 0)) - retry_before
                ),
            }
        )
        return answer


class PlannedDerivedMemoryRule(PropagationRule):
    """Per-edge planned mode over the existing oracle prompt / J1 rule.

    ``direct-stale`` is an invalidation decision, not permission to synthesize
    replacement content.  Semantic recompute requires a separate generator and
    output-validity contract; this planning rule deliberately has neither.
    """

    def __init__(
        self,
        *,
        modes: Sequence[PlannedEdgeMode],
        repo,
        strong_chat,
        cheap_chat,
        event_sink: list | None = None,
        token_ledger: TokenLedger | None = None,
        recompute_on_stale: bool = False,
    ) -> None:
        if recompute_on_stale:
            raise ValueError(
                "recompute_on_stale is unsafe without a semantic recompute "
                "generator and output-validity contract"
            )
        self._modes = {
            (item.dependency_id, item.dependent_id): item.mode for item in modes
        }
        self._repo = repo
        self._event_sink = event_sink if event_sink is not None else []
        self.token_ledger = token_ledger if token_ledger is not None else TokenLedger()
        self.planning_errors: list[dict[str, Any]] = []
        self.executed: list[tuple[UUID, UUID, str]] = []
        self.executed_audit: list[dict[str, Any]] = []
        self._cheap = _UsageChat(cheap_chat)
        self._strong = _UsageChat(strong_chat)
        self._oracle_cascade = DerivedMemoryOracleRule(
            self._strong, repo, cheap_chat=self._cheap, event_sink=self._event_sink
        )
        self._oracle_cheap = DerivedMemoryOracleRule(
            self._cheap, repo, cheap_chat=None, event_sink=self._event_sink
        )
        self._oracle_strong = DerivedMemoryOracleRule(
            self._strong, repo, cheap_chat=None, event_sink=self._event_sink
        )

    def planned_mode_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for mode in self._modes.values():
            mix[mode] = mix.get(mode, 0) + 1
        return mix

    def executed_mode_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for _, _, mode in self.executed:
            mix[mode] = mix.get(mode, 0) + 1
        return mix

    async def evaluate(self, event, target) -> PropagationAction:
        change_type = event.get("change_type", "")
        if change_type not in ("modified", "marked_stale"):
            return PropagationAction(
                action="no_action",
                reason=f"planned rule ignores change_type {change_type}",
            )
        upstream = _as_uuid(event["context_id"])
        dependent = _as_uuid(target["dependent_id"])
        key = (upstream, dependent)
        mode = self._modes.get(key)
        if mode is None:
            self.planning_errors.append(
                {
                    "dependency_id": str(upstream),
                    "dependent_id": str(dependent),
                    "error": "missing_plan",
                }
            )
            action = self._direct_stale(event, reason="planning_error: missing plan")
            self.executed.append((upstream, dependent, "direct-stale"))
            self.executed_audit.append(
                {
                    "dependency_id": str(upstream),
                    "dependent_id": str(dependent),
                    "planned_mode": None,
                    "executed_mode": "direct-stale",
                    "action": action.action,
                    "missing_plan": True,
                    "cheap_tokens": 0,
                    "strong_tokens": 0,
                }
            )
            self.token_ledger.add(key, mode="direct-stale")
            return action

        cheap_before = (self._cheap.prompt_tokens, self._cheap.completion_tokens)
        strong_before = (self._strong.prompt_tokens, self._strong.completion_tokens)
        cheap_call_before = len(self._cheap.call_records)
        strong_call_before = len(self._strong.call_records)
        if mode == "direct-stale":
            action = self._direct_stale(event)
        elif mode == "J1":
            action = await self._evaluate_j1(event, dependent)
        elif mode == "J3":
            action = await self._oracle_cheap.evaluate(event, target)
        elif mode == "J4":
            action = await self._oracle_strong.evaluate(event, target)
        elif mode == "cascade":
            action = await self._oracle_cascade.evaluate(event, target)
        else:
            action = self._direct_stale(event, reason=f"planning_error: unknown {mode}")
            mode = "direct-stale"
        cheap_delta = (
            self._cheap.prompt_tokens - cheap_before[0]
            + self._cheap.completion_tokens - cheap_before[1]
        )
        strong_delta = (
            self._strong.prompt_tokens - strong_before[0]
            + self._strong.completion_tokens - strong_before[1]
        )
        self.executed.append((upstream, dependent, mode))
        self.executed_audit.append(
            {
                "dependency_id": str(upstream),
                "dependent_id": str(dependent),
                "planned_mode": mode,
                "executed_mode": mode,
                "action": action.action,
                "reason": action.reason,
                "missing_plan": False,
                "cheap_tokens": cheap_delta,
                "strong_tokens": strong_delta,
                "cheap_calls": self._cheap.call_records[cheap_call_before:],
                "strong_calls": self._strong.call_records[strong_call_before:],
                "source_version": event.get("source_version")
                or event.get("new_version"),
                "target_version": target.get("target_version"),
                "target_validity_status": target.get("target_validity_status"),
                "parsed_verdict": action.action,
            }
        )
        self.token_ledger.add(
            key, cheap=cheap_delta, strong=strong_delta, mode=mode
        )
        return action

    def _direct_stale(self, event: Mapping[str, Any], reason: str | None = None) -> PropagationAction:
        return PropagationAction(
            action="mark_stale",
            reason=reason or str(event.get("diff_summary") or "planned direct-stale"),
        )

    async def _evaluate_j1(self, event: Mapping[str, Any], dependent: UUID) -> PropagationAction:
        account_id = event["account_id"]
        derived = await self._oracle_strong._fetch_content(account_id, dependent)
        upstream = await self._oracle_strong._describe_change(event, account_id)
        if not derived:
            return PropagationAction(action="no_action", reason="J1: missing derived text")
        if judge_j1(upstream, derived):
            return self._direct_stale(event, reason="J1 rule mark_stale")
        return PropagationAction(action="no_action", reason="J1 rule fresh")


def make_planned_registry(rule: PlannedDerivedMemoryRule) -> PropagationRuleRegistry:
    return PropagationRuleRegistry(
        dep_rules={
            "skill_version": SkillVersionDepRule(),
            "table_schema": TableSchemaRule(),
            "derived_from": rule,
        }
    )


@dataclass
class FrontierRun:
    executed_edges: list[tuple[UUID, UUID, str]]
    planned_mode_mix: dict[str, int]
    executed_mode_mix: dict[str, int]
    realized_tokens: dict[str, int]
    planning_errors: list[dict[str, Any]]
    receding_horizon_complete: bool = False
    continuation_mode: str = CONTINUATION_MODE
    trace: list[dict[str, Any]] = field(default_factory=list)
    unfinished: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RecomputeResult:
    changed: bool
    old_version: int
    new_version: int
    old_semantic_identity: str
    new_semantic_identity: str


async def execute_frontier(
    rule: PlannedDerivedMemoryRule,
    *,
    root_id: UUID,
    adjacency: Mapping[UUID, Sequence[UUID]],
    account_id: str,
    diff_summary: str = "root changed",
    source_version: int = 1,
    recompute: Callable[[UUID, int, Mapping[str, Any]], Awaitable[RecomputeResult]]
    | None = None,
    max_events: int = 256,
    max_depth: int = 64,
) -> FrontierRun:
    """Execute current frontiers and re-plan after real intermediate versions.

    Without ``recompute`` this preserves the old marked-stale proxy behavior.
    With it, a changed recompute creates a new versioned frontier; an unchanged
    semantic identity terminates that branch.
    """

    queue: deque[tuple[str, UUID, int, int, str | None]] = deque(
        [("modified", root_id, source_version, 0, None)]
    )
    seen_events: set[tuple[str, UUID, int]] = set()
    trace: list[dict[str, Any]] = []
    errors: list[str] = []
    event_count = 0
    while queue:
        change_type, node, version, depth, parent = queue.popleft()
        event_count += 1
        if event_count > max_events:
            errors.append(f"event limit exceeded: {max_events}")
            break
        if depth > max_depth:
            errors.append(f"depth limit exceeded: {max_depth}")
            break
        event_key = (change_type, node, version)
        if event_key in seen_events:
            errors.append(f"cycle or duplicate version frontier: {node}@{version}")
            break
        seen_events.add(event_key)
        event_id = f"{node}:{version}:{event_count}"
        event = {
            "event_id": event_id,
            "change_type": change_type,
            "context_id": node,
            "account_id": account_id,
            "diff_summary": diff_summary,
            "source_version": version,
            "parent_event_id": parent,
            "depth": depth,
        }
        trace.append(
            {
                "type": "frontier",
                "event_id": event_id,
                "parent_event_id": parent,
                "node_id": str(node),
                "version": version,
                "depth": depth,
            }
        )
        for dependent in adjacency.get(node, ()):
            action = await rule.evaluate(event, {"dependent_id": dependent})
            trace.append(
                {
                    "type": "edge_action",
                    "event_id": event_id,
                    "dependency_id": str(node),
                    "dependent_id": str(dependent),
                    "source_version": version,
                    "action": action.action,
                }
            )
            if action.action == "mark_stale":
                if recompute is None:
                    queue.append(("marked_stale", dependent, version, depth + 1, event_id))
                    continue
                result = await recompute(dependent, version, event)
                trace.append(
                    {
                        "type": "recompute_result",
                        "event_id": event_id,
                        "node_id": str(dependent),
                        "changed": result.changed,
                        "old_version": result.old_version,
                        "new_version": result.new_version,
                        "old_semantic_identity": result.old_semantic_identity,
                        "new_semantic_identity": result.new_semantic_identity,
                    }
                )
                if result.changed:
                    queue.append(
                        (
                            "modified",
                            dependent,
                            result.new_version,
                            depth + 1,
                            event_id,
                        )
                    )
    return FrontierRun(
        executed_edges=list(rule.executed),
        planned_mode_mix=rule.planned_mode_mix(),
        executed_mode_mix=rule.executed_mode_mix(),
        realized_tokens={
            "cheap_tokens": rule.token_ledger.realized_cheap,
            "strong_tokens": rule.token_ledger.realized_strong,
        },
        planning_errors=list(rule.planning_errors),
        receding_horizon_complete=recompute is not None and not errors,
        continuation_mode=(
            "versioned recompute frontier" if recompute is not None else CONTINUATION_MODE
        ),
        trace=trace,
        unfinished=bool(errors),
        errors=errors,
    )


def p2_case_fields(
    plan_doc: Mapping[str, Any],
    frontier: FrontierRun | None = None,
    *,
    rule: PlannedDerivedMemoryRule | None = None,
) -> dict[str, Any]:
    executed_mix = frontier.executed_mode_mix if frontier is not None else {}
    audit = list(getattr(rule, "executed_audit", []) or [])
    if frontier is None and rule is not None:
        executed_mix = rule.executed_mode_mix()
    runtime_errors = list(frontier.errors if frontier is not None else [])
    missing = len(getattr(rule, "planning_errors", []) or [])
    flags = certification_flags(
        contract_method=str(plan_doc.get("contract_method") or ""),
        feasible=bool(plan_doc.get("feasible", True)),
        planner_infeasible=bool(plan_doc.get("planner_infeasible")),
        missing_assignment_count=missing,
        contract_distribution_mismatch=bool(
            plan_doc.get("contract_distribution_mismatch")
        ),
        evaluation_episodes_in_calibration=bool(
            plan_doc.get("evaluation_episodes_in_calibration")
        ),
        planner_fell_back=bool(plan_doc.get("planner_fell_back")),
        durable_drain_complete=bool(frontier and not frontier.unfinished),
        no_leftover=bool(frontier and not frontier.unfinished),
        no_dead_letter=bool(frontier and not frontier.unfinished),
        receding_horizon_complete=bool(
            frontier and frontier.receding_horizon_complete
        ),
        risk_ledger_complete=bool(plan_doc.get("risk_ledger_complete")),
        continuation_mode=(
            frontier.continuation_mode if frontier is not None else CONTINUATION_MODE
        ),
    )
    if runtime_errors:
        flags["certified"] = False
        flags["held_out_certification"] = False
        blocked = list(flags["certification_blocked_reason"])
        if "unfinished_propagation" not in blocked:
            blocked.append("unfinished_propagation")
        flags["certification_blocked_reason"] = blocked
    ledger = {}
    if rule is not None:
        ledger = {
            "cheap_tokens": rule.token_ledger.realized_cheap,
            "strong_tokens": rule.token_ledger.realized_strong,
            "by_edge": {
                f"{src}->{dst}": dict(payload)
                for (src, dst), payload in rule.token_ledger.by_edge.items()
            },
        }
    return {
        "contract_method": plan_doc.get("contract_method"),
        "epsilon_prop": plan_doc.get("epsilon_prop"),
        "solver": plan_doc.get("solver"),
        "topology": plan_doc.get("topology"),
        "planned_mode_mix": dict(plan_doc.get("planned_mode_mix") or {}),
        "executed_mode_mix": dict(executed_mix),
        "assignment_audit": list(plan_doc.get("assignment_audit") or []),
        "executed_audit": audit,
        "realized_tokens": ledger,
        "planning_errors": list(getattr(rule, "planning_errors", []) or []),
        "max_path_risk": plan_doc.get("max_path_risk", 0.0),
        "direct_stale_count": int(
            (plan_doc.get("planned_mode_mix") or {}).get("direct-stale", 0)
        ),
        "receding_horizon_complete": bool(
            frontier and frontier.receding_horizon_complete
        ),
        "continuation_mode": (
            frontier.continuation_mode if frontier is not None else CONTINUATION_MODE
        ),
        "propagation_trace": list(frontier.trace if frontier is not None else []),
        "unfinished": bool(frontier and frontier.unfinished),
        "runtime_errors": runtime_errors,
        **flags,
    }
