"""Shared symbols for the live memebench pipeline.

This module exists to break "corpse dependencies": several finished experiments
(``run_eval``, ``run_chronological_p1p2``, ``p1_policy_certification``,
``judge_routing_sweep``) each held one or two symbols that live code still
needed, so their files could not be archived even though their own logic no
longer runs.  Every definition below was moved here **byte-for-byte** from the
file named in its section comment; none was rewritten.

Deliberately NOT merged: :func:`clopper_pearson_upper` duplicates
``contexthub.planning.statistics.clopper_pearson_upper`` numerically (verified
equal on 60 random inputs) but not textually -- the production one prefers
SciPy, this one is deliberately SciPy-free.  Merging them would change which
code path a benchmark run takes, so both are kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from uuid import uuid4

from integrations.memebench.systems import EvalSystem

# ``EPSILON_PROP`` is imported lazily inside :func:`bind_root_plan`: this module
# supplies dataclasses to ``chronological_policy``, which owns that constant, so
# a module-level import here would be circular.  Kept as an import rather than a
# copy so the constant still has exactly one definition.


# ----------------------------------------------------------------------------
# MEME data path and per-case token accounting
# moved verbatim from run_eval.py
# ----------------------------------------------------------------------------

DEFAULT_DATA = "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"


_TOKEN_BUCKETS = {
    "ingest_llm": "discovery_chat",
    "inference_llm": "answer_chat",
    "oracle_llm": "oracle_chat",
    "extract_llm": "extract_chat",
    "judge_llm": "judge_chat",
    "cascade_cheap_llm": "cascade_cheap_chat",
    "cascade_strong_llm": "cascade_strong_chat",
    "p2_cheap_llm": "p2_cheap_chat",
}


def _token_snap(system) -> dict:
    """Raw per-bucket counters for delta-ing one case."""
    out = {}
    for bucket, attr in _TOKEN_BUCKETS.items():
        c = getattr(system, attr, None)
        if c is None:
            continue
        out[bucket] = (c.model, c.call_count, c.prompt_tokens, c.completion_tokens)
    return out


def _token_delta(before: dict, after: dict) -> dict:
    """Per-case token usage = after - before, dropping untouched buckets."""
    out = {}
    for bucket, (model, calls, pin, pout) in after.items():
        _m, c0, i0, o0 = before.get(bucket, (model, 0, 0, 0))
        d_calls, d_in, d_out = calls - c0, pin - i0, pout - o0
        if d_calls or d_in or d_out:
            out[bucket] = {"model": model, "calls": d_calls,
                           "prompt_tokens": d_in, "completion_tokens": d_out}
    return out

# ----------------------------------------------------------------------------
# Episode dataclasses, stable split, exact binomial bound
# moved verbatim from p1_policy_certification.py
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class EpisodeResult:
    episode_id: str
    n_gold: int
    n_pred: int
    n_tp: int
    precision: float
    recall: float
    cheap_tokens: float
    strong_tokens: float

    @property
    def graph_miss(self) -> bool:
        return self.n_tp < self.n_gold

    @property
    def total_tokens(self) -> float:
        return self.cheap_tokens + self.strong_tokens


@dataclass(frozen=True)
class PolicyCandidate:
    """A sweep operating point and all of its episode-level observations.

    This intentionally small adapter can be replaced by the planning core's
    ``PolicyCandidate`` once that module lands; certification logic only relies
    on ``policy_id``, ``parameters``, and ``episodes``.
    """

    policy_id: str
    source: str
    parameters: Mapping[str, Any]
    episodes: tuple[EpisodeResult, ...]
    cheap_none: int | None = None


@dataclass(frozen=True)
class EpisodeSplit:
    seed: str
    selection_fraction: float
    selection_ids: tuple[str, ...]
    certification_ids: tuple[str, ...]
    split_hash: str


def stable_episode_split(
    episode_ids: Iterable[str],
    *,
    seed: str,
    selection_fraction: float,
) -> EpisodeSplit:
    """Assign IDs by a stable SHA-256 threshold, independent of input order."""

    if not seed:
        raise ValueError("split seed must be explicit and non-empty")
    if not 0.0 < selection_fraction < 1.0:
        raise ValueError("selection_fraction must be strictly between 0 and 1")
    ids = sorted(str(episode_id) for episode_id in episode_ids)
    if not ids:
        raise ValueError("cannot split an empty episode set")
    if len(ids) != len(set(ids)):
        raise ValueError("episode IDs must be unique")

    cutoff = int(selection_fraction * (1 << 256))
    selection: list[str] = []
    certification: list[str] = []
    for episode_id in ids:
        digest = hashlib.sha256(
            seed.encode("utf-8") + b"\0" + episode_id.encode("utf-8")
        ).digest()
        target = selection if int.from_bytes(digest, "big") < cutoff else certification
        target.append(episode_id)
    if not selection or not certification:
        raise ValueError(
            "stable hash split produced an empty partition; change seed or fraction"
        )

    split_payload = {
        "hash": "sha256(seed + NUL + episode_id)",
        "seed": seed,
        "selection_fraction": selection_fraction,
        "selection_ids": selection,
        "certification_ids": certification,
    }
    split_hash = hashlib.sha256(
        json.dumps(split_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EpisodeSplit(
        seed=seed,
        selection_fraction=selection_fraction,
        selection_ids=tuple(selection),
        certification_ids=tuple(certification),
        split_hash=split_hash,
    )


def clopper_pearson_upper(misses: int, trials: int, alpha: float) -> float:
    """Exact one-sided Clopper-Pearson upper bound for a binomial rate.

    Uses a monotone binomial-CDF bisection, avoiding a scipy dependency.  For
    ``misses < trials``, the returned value solves
    ``P_p[X <= misses] = alpha``.
    """

    if trials < 0 or misses < 0 or misses > trials:
        raise ValueError("require 0 <= misses <= trials")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    if trials == 0 or misses == trials:
        return 1.0
    if misses == 0:
        return 1.0 - alpha ** (1.0 / trials)

    def binomial_cdf(p: float) -> float:
        # Recurrence starts at P(X=0), avoiding factorials.
        q = 1.0 - p
        probability = q**trials
        total = probability
        for k in range(misses):
            probability *= ((trials - k) / (k + 1)) * (p / q)
            total += probability
        return total

    low, high = 0.0, 1.0
    for _ in range(80):
        mid = (low + high) / 2.0
        if binomial_cdf(mid) > alpha:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0

# ----------------------------------------------------------------------------
# Free J1 staleness rule
# moved verbatim from judge_routing_sweep.py
# ----------------------------------------------------------------------------

_PREDECL_RE = re.compile(r"^\s*if\b.{0,80}?\b(will|would|becomes?|changes?|switch(?:es)?)\b",
                         re.IGNORECASE | re.DOTALL)


_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "and", "or", "but", "if", "then", "this",
    "that", "these", "those", "it", "its", "as", "with", "by", "from", "will",
    "would", "my", "i", "you", "he", "she", "they", "we", "user", "s", "their",
    "has", "have", "had", "which", "would", "likely", "change", "changes",
}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 2}


def judge_j1(upstream: str, dependent: str) -> bool:
    """Free rule: dependent shares upstream content words AND is not a predeclaration."""
    if _PREDECL_RE.search(dependent or ""):
        return False
    up_w = _content_words(upstream)
    dep_w = _content_words(dependent)
    return bool(up_w & dep_w)

# ----------------------------------------------------------------------------
# Tenant wipe and frozen-plan binding (runtime behavior)
# moved verbatim from run_chronological_p1p2.py
# ----------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def wipe_account(system: EvalSystem, account: str) -> None:
    """Delete one benchmark tenant using the live FK graph, failing closed."""

    async with system.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.account_id', $1, true)",
                account,
            )
            target_tables = {
                "contexts",
                "change_events",
                "retrieval_trace",
                "context_feedback",
                "document_sections",
                "skill_versions",
                "skill_subscriptions",
                "table_metadata",
                "lineage",
                "table_relationships",
                "query_templates",
                "dependencies",
                "context_relations",
                "context_versions",
                "context_invalidations",
                "propagation_effects",
                "propagation_risk_ledger",
                "propagation_trace",
            }
            fk_rows = await conn.fetch(
                """
                SELECT child.relname AS child_table,
                       parent.relname AS parent_table
                  FROM pg_constraint fk
                  JOIN pg_class child ON child.oid = fk.conrelid
                  JOIN pg_class parent ON parent.oid = fk.confrelid
                  JOIN pg_namespace ns ON ns.oid = child.relnamespace
                 WHERE fk.contype = 'f' AND ns.nspname = 'public'
                """
            )
            graph = {
                (str(row["child_table"]), str(row["parent_table"]))
                for row in fk_rows
                if str(row["child_table"]) in target_tables
                and str(row["parent_table"]) in target_tables
                and row["child_table"] != row["parent_table"]
            }
            required_fk_children = target_tables - {"contexts", "retrieval_trace"}
            observed_fk_children = {child for child, _ in graph}
            missing = sorted(required_fk_children - observed_fk_children)
            if missing:
                raise RuntimeError(
                    "cleanup FK graph is missing expected child tables: "
                    + ", ".join(missing)
                )

            cross_tenant = await conn.fetchval(
                """
                WITH target AS (
                  SELECT id FROM contexts WHERE account_id = $1
                )
                SELECT EXISTS (
                  SELECT 1 FROM change_events e
                   WHERE (e.account_id = $1)
                      <> (e.context_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM dependencies d
                   WHERE (d.dependent_id IN (SELECT id FROM target))
                      <> (d.dependency_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM context_relations r
                   WHERE (r.context_id IN (SELECT id FROM target))
                      <> (r.related_context_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM lineage l
                   WHERE (l.upstream_id IN (SELECT id FROM target))
                      <> (l.downstream_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM table_relationships r
                   WHERE (r.table_id_a IN (SELECT id FROM target))
                      <> (r.table_id_b IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM context_feedback f
                   WHERE (f.account_id = $1)
                      <> (f.context_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM document_sections s
                   WHERE (s.account_id = $1)
                      <> (s.context_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM skill_subscriptions s
                   WHERE (s.account_id = $1)
                      <> (s.skill_id IN (SELECT id FROM target))
                  UNION ALL
                  SELECT 1 FROM propagation_effects p
                    JOIN change_events e ON e.event_id = p.event_id
                   WHERE p.target_context_id IS NOT NULL
                     AND (e.account_id = $1)
                      <> (p.target_context_id IN (SELECT id FROM target))
                )
                """,
                account,
            )
            if cross_tenant:
                raise RuntimeError(
                    f"cleanup refused cross-account FK edge for account {account}"
                )

            predicates = {
                "retrieval_trace": "account_id = $1",
                "context_feedback": "account_id = $1",
                "document_sections": "account_id = $1",
                "skill_subscriptions": "account_id = $1",
                "skill_versions": "skill_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "table_metadata": "context_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "lineage": "upstream_id IN (SELECT id FROM contexts WHERE account_id = $1) OR downstream_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "table_relationships": "table_id_a IN (SELECT id FROM contexts WHERE account_id = $1) OR table_id_b IN (SELECT id FROM contexts WHERE account_id = $1)",
                "query_templates": "context_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "dependencies": "dependent_id IN (SELECT id FROM contexts WHERE account_id = $1) OR dependency_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "context_relations": "context_id IN (SELECT id FROM contexts WHERE account_id = $1) OR related_context_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "context_versions": "context_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "context_invalidations": "cause_event_id IN (SELECT event_id FROM change_events WHERE account_id = $1) OR context_id IN (SELECT id FROM contexts WHERE account_id = $1) OR source_context_id IN (SELECT id FROM contexts WHERE account_id = $1)",
                "propagation_effects": "event_id IN (SELECT event_id FROM change_events WHERE account_id = $1)",
                "propagation_risk_ledger": "event_id IN (SELECT event_id FROM change_events WHERE account_id = $1)",
                "propagation_trace": "event_id IN (SELECT event_id FROM change_events WHERE account_id = $1)",
                "change_events": "account_id = $1",
                "contexts": "account_id = $1",
            }
            remaining = set(predicates)
            delete_order: list[str] = []
            while remaining:
                blocked_parents = {
                    parent
                    for child, parent in graph
                    if child in remaining and parent in remaining
                }
                leaves = sorted(remaining - blocked_parents)
                if not leaves:
                    raise RuntimeError(
                        "cleanup FK graph contains an unsupported non-self cycle"
                    )
                delete_order.extend(leaves)
                remaining.difference_update(leaves)
            for table in delete_order:
                await conn.execute(
                    f"DELETE FROM {table} WHERE {predicates[table]}",
                    account,
                )
            leftovers = {}
            for table in predicates:
                count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {table} WHERE {predicates[table]}",
                    account,
                )
                if count:
                    leftovers[table] = int(count)
            if leftovers:
                raise RuntimeError(f"cleanup left account rows behind: {leftovers}")


def _epsilon_prop() -> float:
    """Fetch the frozen propagation risk budget without a circular import."""

    from integrations.memebench.chronological_policy import EPSILON_PROP

    return EPSILON_PROP


async def bind_root_plan(
    system: EvalSystem,
    account: str,
    plan_doc: Mapping[str, Any],
    contract: Mapping[str, Mapping[str, Any]],
) -> str:
    """Atomically bind every pending root event to one complete frozen plan."""

    assignments = {
        f"{item['dependency_id']}->{item['dependent_id']}": item["mode"]
        for item in plan_doc.get("assignment_audit") or []
    }
    risk = {
        edge: float(contract[mode]["delta"])
        for edge, mode in assignments.items()
    }
    if not assignments or set(assignments) != set(risk):
        raise ValueError("plan binding requires complete assignments and risk deltas")
    plan_id = str(uuid4())
    graph_scope = sha256_text(
        json.dumps(
            {
                "assignments": assignments,
                "source_versions": plan_doc.get("source_versions") or {},
            },
            sort_keys=True,
        )
    )
    metadata = {
        "plan_required": True,
        "assignments": assignments,
        "risk_delta_by_edge": risk,
        "risk_budget": float(plan_doc.get("epsilon_prop") or _epsilon_prop()),
        "graph_scope": graph_scope,
        "source_versions": dict(plan_doc.get("source_versions") or {}),
    }
    async with system.pool.acquire() as conn:
        async with conn.transaction():
            tag = await conn.execute(
                """
                UPDATE change_events
                   SET plan_id = $2::uuid,
                       graph_scope = $3,
                       metadata = coalesce(metadata, '{}'::jsonb) || $4::jsonb,
                       updated_at = NOW()
                 WHERE account_id = $1
                   AND parent_event_id IS NULL
                   AND delivery_status IN ('pending', 'retry')
                """,
                account,
                plan_id,
                graph_scope,
                json.dumps(metadata, sort_keys=True),
            )
            if not tag or tag == "UPDATE 0":
                raise RuntimeError("no pending root event available for plan binding")
    return plan_id
