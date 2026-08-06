"""P1 阶段一 — build-side confidence cascade cost×quality curve (variables 3 & 4).

Sweep the cascade threshold tau and, for each tau, ingest MEME Cascade cases on
PATH B (gold nodes over a filler-inflated pool, so edge P/R is EXACT via edge_pr)
while routing EVERY new node through the two-variable cascade in cascade_router:

  variable 4 (candidate selection): lexical blocking -> embed_topk -> full pool
  variable 3 (edge discovery)      : regex hard-block -> cheap LLM -> strong LLM

For each tau we record the operating point: exact edge precision/recall, the
token cost split by cheap vs strong tier, and the per-tier call mix (what share
of nodes stopped at each rung). Sweeping tau traces the frontier — this is
EVALUATION of a fixed-tau router, not fitting tau on the eval set.

Expected shape (mirrors D.5b propagation cascade): the cascade curve dominates
the single-tier baselines — reaching the strong-only precision at roughly half
the strong-tier token cost, because the cheap tier + regex resolve most nodes and
only the low-confidence ones escalate.

Usage (smoke first with --limit 5):
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.cascade_sweep \
        --hop 1 --limit 5 --provider openlux \
        --cheap-model gpt-4.1-mini --strong-model claude-opus-4-8 \
        --taus 0.0 0.2 0.4 0.6 0.8 1.0 \
        --data /Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json \
        --out integrations/memebench/runs/cascade_curve_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.cascade_router import (
    route_candidate_selection,
    route_edge_discovery,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.candidate_selection_sweep import (
    _filler_texts,
    _gold_specs,
)
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.ingest import (
    IngestedGraph,
    _embed_all,
    _insert_memory,
    edge_pr,
)
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    CascadeCase,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import (
    DEFAULT_PROVIDERS_PATH,
    build_system,
    load_provider,
)


async def ingest_case_cascade(
    db,
    case: CascadeCase,
    account: str,
    embed_batch,
    cheap: DependencyDiscoveryService,
    strong: DependencyDiscoveryService,
    tau: float,
    max_filler: int,
    k: int,
) -> tuple[IngestedGraph, Counter, Counter]:
    """Ingest gold nodes over a filler pool, routing each node through both cascades.

    Path B: gold nodes are the graph under test, filler nodes are pool distractors
    (never scored, never a valid gold source). For each gold node the router picks
    the candidate shortlist (variable 4) then judges edges (variable 3). Returns
    (graph, cand_tier_counts, edge_tier_counts) for the per-tier call mix.
    """
    graph = IngestedGraph(account_id=account, root_id=None)
    node_by_entity: dict[str, uuid.UUID] = {}

    filler_texts = _filler_texts(case, max_filler)
    gold_specs = _gold_specs(case)

    all_texts = filler_texts + [s[1] for s in gold_specs]
    all_emb = await _embed_all(embed_batch, all_texts)
    filler_emb = all_emb[: len(filler_texts)]
    gold_emb = all_emb[len(filler_texts):]

    pool: list[CandidateFact] = []
    for text, emb in zip(filler_texts, filler_emb):
        fid = await _insert_memory(db, account, "filler", text, emb)
        pool.append(CandidateFact(id=fid, text=text, embedding=emb))

    cand_tiers: Counter = Counter()
    edge_tiers: Counter = Counter()
    for (slug, text, role, entity), emb in zip(gold_specs, gold_emb):
        node_id = await _insert_memory(db, account, slug, text, emb)
        graph.node_meta[node_id] = (entity or "", role)
        if role == "root":
            graph.root_id = node_id
            node_by_entity[case.root] = node_id
        elif role == "cur":
            graph.materialized[entity] = node_id
            node_by_entity[entity] = node_id
        elif role == "pre":
            graph.predeclarations[entity] = node_id

        if pool:
            cand = route_candidate_selection(text, emb, pool, tau, k=k)
            cand_tiers[cand.tier] += 1
            edge = await route_edge_discovery(
                text, cand.candidates, tau, cheap=cheap, strong=strong
            )
            edge_tiers[edge.tier] += 1
            for src_id in edge.sources:
                await db.execute(
                    """
                    INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
                    VALUES ($1, $2, 'derived_from')
                    ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
                    """,
                    node_id, src_id,
                )
                graph.persisted_edges.add((src_id, node_id))
        pool.append(CandidateFact(id=node_id, text=text, embedding=emb))

    return graph, cand_tiers, edge_tiers


@dataclass
class CaseResult:
    episode_id: str
    n_gold: int
    n_pred: int
    n_tp: int
    precision: float
    recall: float
    cheap_tokens: int
    strong_tokens: int


@dataclass
class TauResult:
    tau: float
    n_cases: int = 0
    n_error: int = 0
    micro_precision: float = 0.0
    micro_recall: float = 0.0
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    cheap_tokens: int = 0
    strong_tokens: int = 0
    total_tokens: int = 0
    tokens_per_case: float = 0.0
    # share of nodes that stopped at each rung (summed over cases)
    cand_tier_mix: dict = field(default_factory=dict)
    edge_tier_mix: dict = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)


async def run_tau(system, prov, cheap_model, strong_model, tau, cases, max_filler, k) -> TauResult:
    """Ingest all cases at a fixed tau; exact edge P/R + tier-split token cost."""
    cheap_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=cheap_model)
    )
    strong_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=strong_model)
    )
    # Plain services (no conditional flags): the router owns tier-0 regex, applied
    # exactly once, so the services must not also hard-block.
    cheap = DependencyDiscoveryService(cheap_chat)
    strong = DependencyDiscoveryService(strong_chat)
    embed_batch = system.embedding.embed_batch

    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    res = TauResult(tau=tau)
    sum_tp = sum_pred = sum_gold = 0
    cand_mix: Counter = Counter()
    edge_mix: Counter = Counter()
    for i, case in enumerate(cases, 1):
        account = f"casc-{tau:.2f}-{case.episode_id}-{case.target_entity}"[:60]
        try:
            c0, s0 = cheap_chat.total_tokens, strong_chat.total_tokens
            async with system.repo.session(account) as db:
                graph, ct, et = await ingest_case_cascade(
                    db, case, account, embed_batch, cheap, strong, tau, max_filler, k
                )
                pr = edge_pr(case, graph)
            ctok = cheap_chat.total_tokens - c0
            stok = strong_chat.total_tokens - s0
            cand_mix.update(ct)
            edge_mix.update(et)
            res.cases.append(CaseResult(
                episode_id=case.episode_id,
                n_gold=pr["n_gold"], n_pred=pr["n_pred"], n_tp=pr["n_tp"],
                precision=pr["precision"], recall=pr["recall"],
                cheap_tokens=ctok, strong_tokens=stok,
            ))
            sum_tp += pr["n_tp"]; sum_pred += pr["n_pred"]; sum_gold += pr["n_gold"]
            res.cheap_tokens += ctok; res.strong_tokens += stok
            print(f"  [tau={tau:.2f}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"P={pr['precision']:.2f} R={pr['recall']:.2f} "
                  f"cheap={ctok} strong={stok}", flush=True)
        except Exception as exc:
            res.n_error += 1
            print(f"  [tau={tau:.2f}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"ERROR {type(exc).__name__}: {exc}", flush=True)

    await cheap_chat.close()
    await strong_chat.close()
    ok = res.cases
    res.n_cases = len(ok)
    res.micro_precision = (sum_tp / sum_pred) if sum_pred else 0.0
    res.micro_recall = (sum_tp / sum_gold) if sum_gold else 0.0
    res.macro_precision = (sum(c.precision for c in ok) / len(ok)) if ok else 0.0
    res.macro_recall = (sum(c.recall for c in ok) / len(ok)) if ok else 0.0
    res.total_tokens = res.cheap_tokens + res.strong_tokens
    res.tokens_per_case = (res.total_tokens / len(ok)) if ok else 0.0
    res.cand_tier_mix = dict(cand_mix)
    res.edge_tier_mix = dict(edge_mix)
    return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None)
    ap.add_argument("--limit", type=int, default=40, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH),
                    help="use meme_filler32k.json so the candidate pool is large")
    ap.add_argument("--provider", default="openlux")
    ap.add_argument("--cheap-model", default="gpt-4.1-mini")
    ap.add_argument("--strong-model", default="claude-opus-4-8")
    ap.add_argument("--taus", nargs="+", type=float,
                    default=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ap.add_argument("--k", type=int, default=10, help="embed_topk width (variable 4)")
    ap.add_argument("--max-filler", type=int, default=60,
                    help="cap filler pool nodes per case (keeps full-tier runnable)")
    ap.add_argument("--out", default="integrations/memebench/runs/cascade_curve.json")
    args = ap.parse_args()

    system = await build_system(provider_label=args.provider, chat_model=args.cheap_model)
    prov = load_provider(args.provider, DEFAULT_PROVIDERS_PATH)
    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.limit:
        cases = cases[: args.limit]

    results: list[TauResult] = []
    try:
        for tau in args.taus:
            print(f"=== tau: {tau:.2f} ({len(cases)} cases, "
                  f"cheap={args.cheap_model} strong={args.strong_model}) ===", flush=True)
            results.append(await run_tau(
                system, prov, args.cheap_model, args.strong_model,
                tau, cases, args.max_filler, args.k,
            ))
    finally:
        await system.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"provider": args.provider, "cheap_model": args.cheap_model,
         "strong_model": args.strong_model, "hop": args.hop,
         "n_cases": len(cases), "max_filler": args.max_filler, "k": args.k,
         "results": [asdict(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 84)
    print(f"Build-side cascade cost×quality  (hop={args.hop or 'all'}, {len(cases)} cases, "
          f"cheap={args.cheap_model} strong={args.strong_model})")
    print("=" * 84)
    print(f"{'tau':>5} {'micro-P':>8} {'micro-R':>8} {'macro-P':>8} {'macro-R':>8} "
          f"{'cheap-tok':>10} {'strong-tok':>11} {'tok/case':>9}")
    for r in results:
        print(f"{r.tau:>5.2f} {r.micro_precision:>8.3f} {r.micro_recall:>8.3f} "
              f"{r.macro_precision:>8.3f} {r.macro_recall:>8.3f} "
              f"{r.cheap_tokens:>10} {r.strong_tokens:>11} {r.tokens_per_case:>9.0f}"
              + (f"  ({r.n_error} err)" if r.n_error else ""))
    for r in results:
        print(f"  tau={r.tau:.2f}  cand-mix={r.cand_tier_mix}  edge-mix={r.edge_tier_mix}")
    print(f"\nArtifacts -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
