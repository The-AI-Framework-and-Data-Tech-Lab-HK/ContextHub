"""Frozen P1 build-plan menu and episode split for the chronological MEME harness.

This module is benchmark-only. It does not choose a new policy on evaluation
data, does not add plans at runtime, and does not touch production schema.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal, Mapping, Sequence
from uuid import UUID

from contexthub.services.dependency_discovery_service import CandidateFact
from integrations.memebench.common import (
    EpisodeResult,
    EpisodeSplit,
    PolicyCandidate,
    stable_episode_split,
)

SPLIT_SEED = "meme-chronological-v1"
SELECTION_FRACTION = 0.60
EVALUATION_FRACTION = 0.40
EPSILON_GRAPH = 0.05
EPSILON_PROP = 0.10
ALPHA_GRAPH = 0.025
ALPHA_PROP = 0.025
ALPHA_TOTAL = 0.05
P1_SELECTION_GRAPH_MISS_LIMIT = 0.20
RECOMPUTE_COST_PRIMARY = 1000.0
RECOMPUTE_COST_SENSITIVITY = (100.0, 1000.0, 10000.0)
MICROBATCH_K = 5
NO_ELIGIBLE_P1_POLICY = "No eligible P1 policy"

ConsolidationMode = Literal[
    "sync-inline",
    "async-each-session",
    "async-microbatch",
    "backfill",
]


@dataclass(frozen=True)
class BuildPlan:
    name: str
    tau_disamb: float
    tau_cand: float
    p_min: float | None
    lam: float | None
    tau_edge: float | None
    k: int
    recency: bool


@dataclass(frozen=True)
class ConsolidationSchedule:
    mode: ConsolidationMode
    batch_size: int


@dataclass
class PendingFact:
    node_id: UUID
    text: str
    embedding: list[float] | None
    session_index: int
    candidate_snapshot: tuple[CandidateFact, ...]
    enqueued_at: float


def _finite_or_inf(value: float | None, *, field: str, allow_none: bool) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} must not be None")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if math.isnan(number):
        raise ValueError(f"{field} must not be NaN")
    return number


def validate_build_plan(plan: BuildPlan) -> None:
    """Reject mixed ``tau_edge`` / ``p_min`` semantics and incomplete templates."""

    if not plan.name:
        raise ValueError("build plan name must be non-empty")
    if plan.k < 1:
        raise ValueError("k must be >= 1")
    if not isinstance(plan.recency, bool):
        raise ValueError("recency must be a bool")
    _finite_or_inf(plan.tau_disamb, field="tau_disamb", allow_none=False)
    _finite_or_inf(plan.tau_cand, field="tau_cand", allow_none=False)

    if plan.lam is None:
        if plan.tau_edge is None:
            raise ValueError("lam is None requires tau_edge; do not mix with p_min")
        if plan.p_min is not None:
            raise ValueError("lam is None forbids p_min; tau_edge is the discovery threshold")
        _finite_or_inf(plan.tau_edge, field="tau_edge", allow_none=False)
        return

    if plan.p_min is None:
        raise ValueError("lam requires p_min as the route_edge_discovery floor")
    if plan.tau_edge is not None:
        raise ValueError("lam forbids tau_edge; map p_min onto the function's tau argument")
    _finite_or_inf(plan.p_min, field="p_min", allow_none=False)
    _finite_or_inf(plan.lam, field="lam", allow_none=False)


def registered_build_plans() -> dict[str, BuildPlan]:
    """Return the five frozen complete templates. Evaluation cannot add plans."""

    plans = (
        BuildPlan(
            name="E_economy",
            tau_disamb=0.0,
            tau_cand=0.0,
            p_min=0.0,
            lam=math.inf,
            tau_edge=None,
            k=5,
            recency=True,
        ),
        BuildPlan(
            name="T_current_tau",
            tau_disamb=0.5,
            tau_cand=0.4,
            p_min=None,
            lam=None,
            tau_edge=0.4,
            k=5,
            recency=True,
        ),
        BuildPlan(
            name="B_lambda",
            tau_disamb=0.5,
            tau_cand=0.4,
            p_min=0.0,
            lam=1e-4,
            tau_edge=None,
            k=5,
            recency=True,
        ),
        BuildPlan(
            name="R_full_cheap",
            tau_disamb=0.5,
            tau_cand=1.0,
            p_min=0.0,
            lam=math.inf,
            tau_edge=None,
            k=5,
            recency=True,
        ),
        BuildPlan(
            name="R_full_verify",
            tau_disamb=1.0,
            tau_cand=1.0,
            p_min=0.0,
            lam=0.0,
            tau_edge=None,
            k=5,
            recency=True,
        ),
    )
    names = [plan.name for plan in plans]
    if len(names) != len(set(names)):
        raise RuntimeError("registered build plan names must be unique")
    for plan in plans:
        validate_build_plan(plan)
    return {plan.name: plan for plan in plans}


def registered_schedules() -> dict[str, ConsolidationSchedule]:
    return {
        "sync-inline": ConsolidationSchedule(mode="sync-inline", batch_size=1),
        "async-each-session": ConsolidationSchedule(
            mode="async-each-session", batch_size=1
        ),
        "async-microbatch-k5": ConsolidationSchedule(
            mode="async-microbatch", batch_size=MICROBATCH_K
        ),
        "backfill": ConsolidationSchedule(mode="backfill", batch_size=0),
    }


def edge_discovery_args(plan: BuildPlan) -> tuple[float, float | None]:
    """Map DTO fields onto ``route_edge_discovery(tau, lam=...)``."""

    validate_build_plan(plan)
    if plan.lam is None:
        assert plan.tau_edge is not None
        return plan.tau_edge, None
    assert plan.p_min is not None
    return plan.p_min, plan.lam


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError("NaN is not allowed in chronological JSON")
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def build_plan_to_json(plan: BuildPlan) -> dict[str, Any]:
    validate_build_plan(plan)
    return json_safe(asdict(plan))


def _parse_optional_float(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if value == "Infinity":
        return math.inf
    if value == "-Infinity":
        return -math.inf
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} must be numeric or an Infinity string")
    number = float(value)
    if math.isnan(number):
        raise ValueError(f"{field} must not be NaN")
    return number


def build_plan_from_json(payload: Mapping[str, Any]) -> BuildPlan:
    plan = BuildPlan(
        name=str(payload["name"]),
        tau_disamb=float(payload["tau_disamb"]),
        tau_cand=float(payload["tau_cand"]),
        p_min=_parse_optional_float(payload.get("p_min"), field="p_min"),
        lam=_parse_optional_float(payload.get("lam"), field="lam"),
        tau_edge=_parse_optional_float(payload.get("tau_edge"), field="tau_edge"),
        k=int(payload["k"]),
        recency=bool(payload["recency"]),
    )
    validate_build_plan(plan)
    return plan


def plan_menu_manifest() -> dict[str, Any]:
    plans = registered_build_plans()
    return {
        "plans": [build_plan_to_json(plans[name]) for name in sorted(plans)],
        "schedules": {
            name: asdict(schedule)
            for name, schedule in registered_schedules().items()
        },
        "split_seed": SPLIT_SEED,
        "selection_fraction": SELECTION_FRACTION,
        "evaluation_fraction": EVALUATION_FRACTION,
        "epsilon_graph": EPSILON_GRAPH,
        "epsilon_prop": EPSILON_PROP,
        "alpha_graph": ALPHA_GRAPH,
        "alpha_prop": ALPHA_PROP,
        "alpha_total": ALPHA_TOTAL,
        "p1_selection_graph_miss_limit": P1_SELECTION_GRAPH_MISS_LIMIT,
        "recompute_cost_primary": RECOMPUTE_COST_PRIMARY,
        "recompute_cost_sensitivity": list(RECOMPUTE_COST_SENSITIVITY),
        "microbatch_k": MICROBATCH_K,
    }


def policy_manifest_hash(payload: Mapping[str, Any] | None = None) -> str:
    document = payload if payload is not None else plan_menu_manifest()
    encoded = json.dumps(
        json_safe(document), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def split_episodes_for_chronological(
    episode_ids: Iterable[str],
    *,
    seed: str = SPLIT_SEED,
    selection_fraction: float = SELECTION_FRACTION,
) -> EpisodeSplit:
    """Stable SHA-256 episode split; same episode never crosses the cut."""

    return stable_episode_split(
        episode_ids, seed=seed, selection_fraction=selection_fraction
    )


def assert_frozen_plan_menu(plan_names: Iterable[str]) -> None:
    allowed = set(registered_build_plans())
    names = set(plan_names)
    extra = names - allowed
    if extra:
        raise ValueError(
            f"evaluation cannot add P1 plans; extra={sorted(extra)}"
        )
    missing = allowed - names
    if missing:
        raise ValueError(
            f"frozen P1 menu is incomplete; missing={sorted(missing)}"
        )


def case_is_scored(case: Mapping[str, Any]) -> bool:
    if case.get("errors"):
        return False
    p1 = case.get("p1") if isinstance(case.get("p1"), Mapping) else case
    if isinstance(p1, Mapping) and p1.get("scored") is False:
        return False
    return True


def _case_token_pair(case: Mapping[str, Any]) -> tuple[float, float]:
    p1 = case.get("p1") if isinstance(case.get("p1"), Mapping) else case
    tokens = case.get("tokens") if isinstance(case.get("tokens"), Mapping) else {}
    cheap = float(
        p1.get(
            "cheap_tokens",
            (tokens.get("cascade_cheap_llm") or {}).get("prompt_tokens", 0)
            + (tokens.get("cascade_cheap_llm") or {}).get("completion_tokens", 0),
        )
        if isinstance(p1, Mapping)
        else 0.0
    )
    strong = float(
        p1.get(
            "strong_tokens",
            (tokens.get("cascade_strong_llm") or {}).get("prompt_tokens", 0)
            + (tokens.get("cascade_strong_llm") or {}).get("completion_tokens", 0),
        )
        if isinstance(p1, Mapping)
        else 0.0
    )
    return cheap, strong


def case_to_episode_result(case: Mapping[str, Any]) -> EpisodeResult:
    p1 = case.get("p1") if isinstance(case.get("p1"), Mapping) else case
    scored = case_is_scored(case)
    n_gold = int(p1.get("n_gold") or 0)
    n_pred = int(p1.get("n_pred") or 0)
    n_tp = int(p1.get("n_tp") or 0)
    if not scored:
        n_gold = max(n_gold, 1)
        n_tp = 0
    precision = n_tp / n_pred if n_pred else 0.0
    recall = n_tp / n_gold if n_gold else 0.0
    cheap, strong = _case_token_pair(case)
    return EpisodeResult(
        episode_id=str(case["episode_id"]),
        n_gold=n_gold,
        n_pred=n_pred,
        n_tp=n_tp,
        precision=precision,
        recall=recall,
        cheap_tokens=cheap,
        strong_tokens=strong,
    )


def collapse_cases_to_episodes(
    cases: Sequence[Mapping[str, Any]],
) -> tuple[EpisodeResult, ...]:
    """One observation per episode: any case miss or failure is an episode miss."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case["episode_id"])].append(case)
    results: list[EpisodeResult] = []
    for episode_id, group in grouped.items():
        n_gold = n_pred = n_tp = 0
        cheap = strong = 0.0
        failed = False
        for case in group:
            cheap_i, strong_i = _case_token_pair(case)
            cheap += cheap_i
            strong += strong_i
            if not case_is_scored(case):
                failed = True
                continue
            p1 = case.get("p1") if isinstance(case.get("p1"), Mapping) else case
            n_gold += int(p1.get("n_gold") or 0)
            n_pred += int(p1.get("n_pred") or 0)
            n_tp += int(p1.get("n_tp") or 0)
        if failed:
            n_gold = max(n_gold, 1)
            n_tp = 0
        precision = n_tp / n_pred if n_pred else 0.0
        recall = n_tp / n_gold if n_gold else 0.0
        results.append(
            EpisodeResult(
                episode_id=episode_id,
                n_gold=n_gold,
                n_pred=n_pred,
                n_tp=n_tp,
                precision=precision,
                recall=recall,
                cheap_tokens=cheap,
                strong_tokens=strong,
            )
        )
    return tuple(sorted(results, key=lambda item: item.episode_id))


def policy_from_selection_cases(
    plan: BuildPlan,
    cases: Sequence[Mapping[str, Any]],
) -> PolicyCandidate:
    validate_build_plan(plan)
    episodes = collapse_cases_to_episodes(cases)
    cheap_none = sum(
        int((case.get("p1") if isinstance(case.get("p1"), Mapping) else case).get("cheap_none", 0))
        for case in cases
        if case_is_scored(case)
    )
    return PolicyCandidate(
        policy_id=plan.name,
        source="chronological_p1_selection",
        parameters=build_plan_to_json(plan),
        episodes=episodes,
        cheap_none=cheap_none,
    )


def select_frozen_p1_policy(
    policies: Sequence[PolicyCandidate],
    split: EpisodeSplit,
    *,
    selection_risk_limit: float = P1_SELECTION_GRAPH_MISS_LIMIT,
) -> dict[str, Any]:
    """Choose one complete plan on the selection split only.

    Evaluation observations are ignored. If nothing meets the feasibility
    admission threshold, the result is ``No eligible P1 policy`` rather than a
    silently relaxed cap.
    """

    assert_frozen_plan_menu(policy.policy_id for policy in policies)
    selection_ids = set(split.selection_ids)
    menu: list[dict[str, Any]] = []
    eligible: list[tuple[float, float, str, PolicyCandidate]] = []
    for policy in policies:
        selected = [ep for ep in policy.episodes if ep.episode_id in selection_ids]
        if len({ep.episode_id for ep in selected}) != len(selected):
            raise ValueError(
                f"{policy.policy_id} has duplicate case-level episode observations; "
                "collapse to one row per episode before selection"
            )
        missing = selection_ids - {ep.episode_id for ep in selected}
        if missing:
            raise ValueError(
                f"{policy.policy_id} missing selection episodes: {sorted(missing)[:5]}"
            )
        extra = {ep.episode_id for ep in policy.episodes} - (
            set(split.selection_ids) | set(split.certification_ids)
        )
        if extra:
            raise ValueError(
                f"{policy.policy_id} has episodes outside the frozen split"
            )
        misses = sum(ep.graph_miss for ep in selected)
        tokens = sum(ep.total_tokens for ep in selected)
        miss_rate = misses / len(selected) if selected else 1.0
        tokens_per = tokens / len(selected) if selected else math.inf
        n_gold_edges = sum(ep.n_gold for ep in selected)
        n_recalled_edges = sum(min(ep.n_tp, ep.n_gold) for ep in selected)
        n_missed_edges = n_gold_edges - n_recalled_edges
        risk_ok = miss_rate <= selection_risk_limit
        entry = {
            "policy_id": policy.policy_id,
            "parameters": dict(policy.parameters),
            "selection_graph_miss_rate": miss_rate,
            "selection_edge_miss_rate": (
                n_missed_edges / n_gold_edges if n_gold_edges else None
            ),
            "selection_n_gold_edges": n_gold_edges,
            "selection_n_missed_edges": n_missed_edges,
            "selection_tokens_per_episode": tokens_per,
            "selection_risk_ok": risk_ok,
        }
        menu.append(entry)
        if risk_ok:
            eligible.append((tokens_per, miss_rate, policy.policy_id, policy))

    selected_policy = min(eligible)[3] if eligible else None
    return {
        "decision": (
            selected_policy.policy_id if selected_policy is not None else NO_ELIGIBLE_P1_POLICY
        ),
        "frozen": selected_policy is not None,
        "selection_risk_limit": selection_risk_limit,
        "split_hash": split.split_hash,
        "policy_menu": menu,
        "selected_policy": (
            {
                "policy_id": selected_policy.policy_id,
                "parameters": dict(selected_policy.parameters),
                "source": selected_policy.source,
            }
            if selected_policy is not None
            else None
        ),
        "smoke_fallback_plan": "T_current_tau",
        "note": (
            "T_current_tau may be run as a mechanism smoke if no policy is "
            "eligible; that smoke is not a new-P1 claim."
            if selected_policy is None
            else "Policy frozen from selection split; timing ablation must not re-select."
        ),
    }
