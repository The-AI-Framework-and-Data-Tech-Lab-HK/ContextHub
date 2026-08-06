"""P1 阶段二 — build-side cascade over ALL FOUR variables, on the RAW-dialogue path.

Harness 2 (complement to the exact path-B harness in cascade_sweep.py). Here the
input is raw conversation text, so every build step actually runs and all four
cascade variables have a real job:

  variable 1 (extraction)          : regex/template -> weak LLM -> strong LLM
  variable 2 (coref/disambiguation): literal/alias -> spaCy NER -> LLM
  variable 4 (candidate selection) : lexical blocking -> embed_topk -> full pool
  variable 3 (edge discovery)      : regex hard-block -> cheap LLM -> strong LLM

The cost is that edge P/R is APPROXIMATE (edge_pr_raw maps self-extracted nodes
back to gold entities by value — no gold entity labels on this path). So path B
(cascade_sweep.py) stays the exact main curve for variables 3+4; this harness
shows the same cascade mechanism holds end-to-end on real extraction, across all
four variables, with the per-tier call mix as the primary evidence.

Honesty (same as path B): tau is fixed at design time, never tuned on the eval
set; every confidence signal is runtime-observable and gold-free; sweeping tau is
EVALUATION of a fixed-tau router, not fitting.

Usage (smoke first with --limit 3):
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.cascade_raw_sweep \
        --hop 1 --limit 3 --provider openlux \
        --cheap-model gpt-4.1-mini --strong-model claude-opus-4-8 \
        --taus 0.0 0.5 1.0 \
        --data /Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json \
        --out integrations/memebench/runs/cascade_raw_curve_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.cascade_router import (
    _PRONOUN,
    _spacy_nlp,
    route_candidate_selection,
    route_disambiguation,
    route_edge_discovery,
    route_extraction,
)
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.ingest import (
    IngestedGraph,
    _embed_all,
    _insert_memory,
    _session_text,
    _split_evidence,
    edge_pr_raw,
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


def _proper_names(text: str) -> list[str]:
    """Named-entity spans (PERSON/ORG/GPE) in a fact, via the shared spaCy singleton.

    Builds the running entity pool for variable-2 disambiguation. Gold-free — the
    pool is whatever the extractor's own outputs named, never case.entities.
    """
    doc = _spacy_nlp()(text or "")
    return [s.text.strip() for s in doc.ents if s.label_ in ("PERSON", "ORG", "GPE")]


async def ingest_case_raw_cascade(
    db,
    case: CascadeCase,
    account_id: str,
    embed_batch,
    *,
    extract_cheap: ConversationExtractionService,
    extract_strong: ConversationExtractionService,
    disamb_llm: DependencyDiscoveryService,
    edge_cheap: DependencyDiscoveryService,
    edge_strong: DependencyDiscoveryService,
    tau: float,
    k: int,
) -> tuple[IngestedGraph, dict[str, Counter]]:
    """Ingest one case from raw dialogue, routing all four variables through cascades.

    Mirrors ingest_case_raw's timeline-incremental shape but replaces each single
    tier with its cascade. Does NOT touch the shared ingest_case_raw. Returns
    (graph, {variable -> tier Counter}).
    """
    graph = IngestedGraph(account_id=account_id, root_id=None)
    pre_sessions, _ = _split_evidence(case)

    pool: list[CandidateFact] = []
    entity_pool: list[str] = []
    tiers = {"extract": Counter(), "disamb": Counter(), "cand": Counter(), "edge": Counter()}

    for sess in pre_sessions:
        # variable 1: extraction cascade over the whole session.
        ext = await route_extraction(
            _session_text(sess), tau, cheap=extract_cheap, strong=extract_strong
        )
        tiers["extract"][ext.tier] += 1
        texts = [f.text for f in ext.facts if f.text and f.text.strip()]
        if not texts:
            continue

        embeddings = await _embed_all(embed_batch, texts)
        for text, emb in zip(texts, embeddings):
            # variable 2: resolve the first pronoun mention against the entity pool.
            m = _PRONOUN.search(text)
            if m and entity_pool:
                dis = await route_disambiguation(
                    m.group(0), text, entity_pool, tau, llm=disamb_llm
                )
                tiers["disamb"][dis.tier] += 1
                judge_text = f"{text} ({m.group(0)} = {dis.resolution})" if dis.resolution else text
            else:
                tiers["disamb"]["none"] += 1
                judge_text = text

            new_id = await _insert_memory(db, account_id, "fact", text, emb)

            if pool:
                # variable 4: candidate selection cascade.
                cand = route_candidate_selection(judge_text, emb, pool, tau, k=k)
                tiers["cand"][cand.tier] += 1
                # variable 3: edge discovery cascade.
                edge = await route_edge_discovery(
                    judge_text, cand.candidates, tau, cheap=edge_cheap, strong=edge_strong
                )
                tiers["edge"][edge.tier] += 1
                for src_id in edge.sources:
                    await db.execute(
                        """
                        INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
                        VALUES ($1, $2, 'derived_from')
                        ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
                        """,
                        new_id, src_id,
                    )
                    graph.persisted_edges.add((src_id, new_id))

            graph.inserted_nodes.append((new_id, text))
            pool.append(CandidateFact(id=new_id, text=text, embedding=emb))
            for name in _proper_names(text):
                if name not in entity_pool:
                    entity_pool.append(name)

    return graph, tiers


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
    extract_tier_mix: dict = field(default_factory=dict)
    disamb_tier_mix: dict = field(default_factory=dict)
    cand_tier_mix: dict = field(default_factory=dict)
    edge_tier_mix: dict = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)


async def run_tau(system, prov, cheap_model, strong_model, tau, cases, k) -> TauResult:
    """Ingest all cases at fixed tau on the raw path; approx edge P/R + tier mix."""
    cheap_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=cheap_model)
    )
    strong_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=strong_model)
    )
    extract_cheap = ConversationExtractionService(cheap_chat)
    extract_strong = ConversationExtractionService(strong_chat)
    disamb_llm = DependencyDiscoveryService(cheap_chat)  # disamb escalation on cheap tier
    edge_cheap = DependencyDiscoveryService(cheap_chat)
    edge_strong = DependencyDiscoveryService(strong_chat)
    embed_batch = system.embedding.embed_batch

    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    res = TauResult(tau=tau)
    sum_tp = sum_pred = sum_gold = 0
    mix = {"extract": Counter(), "disamb": Counter(), "cand": Counter(), "edge": Counter()}
    for i, case in enumerate(cases, 1):
        account = f"rawc-{tau:.2f}-{case.episode_id}-{case.target_entity}"[:60]
        try:
            c0, s0 = cheap_chat.total_tokens, strong_chat.total_tokens
            async with system.repo.session(account) as db:
                graph, tiers = await ingest_case_raw_cascade(
                    db, case, account, embed_batch,
                    extract_cheap=extract_cheap, extract_strong=extract_strong,
                    disamb_llm=disamb_llm, edge_cheap=edge_cheap, edge_strong=edge_strong,
                    tau=tau, k=k,
                )
                pr = edge_pr_raw(case, graph)
            ctok = cheap_chat.total_tokens - c0
            stok = strong_chat.total_tokens - s0
            for key, ctr in tiers.items():
                mix[key].update(ctr)
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
    res.extract_tier_mix = dict(mix["extract"])
    res.disamb_tier_mix = dict(mix["disamb"])
    res.cand_tier_mix = dict(mix["cand"])
    res.edge_tier_mix = dict(mix["edge"])
    return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None)
    ap.add_argument("--limit", type=int, default=20, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    ap.add_argument("--provider", default="openlux")
    ap.add_argument("--cheap-model", default="gpt-4.1-mini")
    ap.add_argument("--strong-model", default="claude-opus-4-8")
    ap.add_argument("--taus", nargs="+", type=float,
                    default=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ap.add_argument("--k", type=int, default=10, help="embed_topk width (variable 4)")
    ap.add_argument("--out", default="integrations/memebench/runs/cascade_raw_curve.json")
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
            print(f"=== tau: {tau:.2f} ({len(cases)} cases, raw path, "
                  f"cheap={args.cheap_model} strong={args.strong_model}) ===", flush=True)
            results.append(await run_tau(
                system, prov, args.cheap_model, args.strong_model, tau, cases, args.k,
            ))
    finally:
        await system.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"provider": args.provider, "cheap_model": args.cheap_model,
         "strong_model": args.strong_model, "hop": args.hop, "path": "raw",
         "n_cases": len(cases), "k": args.k,
         "results": [asdict(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 84)
    print(f"Build-side cascade (raw path, all 4 vars, APPROX edge P/R)  "
          f"hop={args.hop or 'all'}, {len(cases)} cases")
    print("=" * 84)
    print(f"{'tau':>5} {'micro-P':>8} {'micro-R':>8} {'macro-P':>8} {'macro-R':>8} "
          f"{'cheap-tok':>10} {'strong-tok':>11} {'tok/case':>9}")
    for r in results:
        print(f"{r.tau:>5.2f} {r.micro_precision:>8.3f} {r.micro_recall:>8.3f} "
              f"{r.macro_precision:>8.3f} {r.macro_recall:>8.3f} "
              f"{r.cheap_tokens:>10} {r.strong_tokens:>11} {r.tokens_per_case:>9.0f}"
              + (f"  ({r.n_error} err)" if r.n_error else ""))
    for r in results:
        print(f"  tau={r.tau:.2f} extract={r.extract_tier_mix} disamb={r.disamb_tier_mix} "
              f"cand={r.cand_tier_mix} edge={r.edge_tier_mix}")
    print(f"\nArtifacts -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
