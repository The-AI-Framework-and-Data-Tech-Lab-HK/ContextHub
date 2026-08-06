"""Task ③ — candidate-selection cost×quality curve: sweep the CANDIDATE FILTER
while holding the discovery model fixed, on PATH B (gold nodes) so edge P/R is
EXACT.

The knob
--------
When a new fact arrives, discovery must decide which already-stored fact(s) it is
derived from. The naive path (ingest._discovered_edges) feeds ALL earlier nodes
into one LLM prompt — so each of N new facts is judged against O(N) candidates,
and the prompt-token total is O(N^2). Candidate selection replaces "all earlier
nodes" with a cheaper shortlist:

  - full        : every earlier node (the O(N^2) baseline).
  - embed_topk  : the top-k earlier nodes by cosine to the new fact's embedding.
                  Embeddings are the ones already computed at insert time, so the
                  SELECTION itself costs no extra LLM/embedding call — it only
                  shrinks the prompt. O(N*k) = O(N).
  - lexical     : earlier nodes sharing >=1 content word with the new fact
                  (zero-vector blocking, no embedding needed at all).

Why a big candidate pool (filler)
---------------------------------
MEME's gold nodes alone are ~5-15 per case; on that tiny pool full vs top-k is
indistinguishable and the curve degenerates to a flat line. The O(N^2) blow-up
only appears when the pool is large, which is the REAL write-path predicament:
a new fact's upstream must be found among the whole store (evidence + hundreds of
unrelated chatter nodes). So we ingest MEME's filler sessions as competing pool
nodes (use meme_filler32k.json) and let discovery search the full store.

The reused hypothesis (embedding as RETRIEVER, not JUDGE)
---------------------------------------------------------
Stage A showed embedding cosine is a WEAK judge (real vs false edges both ~0.9,
no separation). But as a RETRIEVER it need not separate true from false — only
rank the true upstream into the top-k. And the 52% purely-semantic negatives from
the neg-edge set (driving/pottery/partner-James mislinks) are LOW-similarity, so
embed_topk should keep them OUT of the candidate set entirely — never giving the
LLM the chance to mislink. Prediction: embed_topk cuts tokens AND lifts precision.

This is a controlled internal ablation (path B, exact edge_pr), not gold-as-input
gaming — the same reasoning as the discovery-model sweep (②).

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.candidate_selection_sweep \
        --hop 1 --limit 40 --provider openlux --model gpt-4o-mini \
        --data /Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json \
        --filters full embed_topk:5 embed_topk:10 embed_topk:20 lexical \
        --out integrations/memebench/runs/cand_sweep_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.ingest import (
    IngestedGraph,
    _current_value_fact,
    _embed_all,
    _gold_facts_for,
    _insert_memory,
    _predeclaration_fact,
    _session_text,
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

# ---------------------------------------------------------------------------
# Candidate filters: given the new node's (text, embedding) and the pool of
# already-inserted (id, text, embedding), return the shortlist to send the LLM.
# Every filter takes the SAME full pool; only what it forwards differs.
# ---------------------------------------------------------------------------

_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "and", "or", "but", "if", "then", "this",
    "that", "these", "those", "it", "its", "as", "with", "by", "from", "will",
    "would", "my", "i", "you", "he", "she", "they", "we", "user", "s",
}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def _cosine(a, b) -> float:
    if a is None or b is None:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


@dataclass
class PoolNode:
    id: uuid.UUID
    text: str
    embedding: list | None


def select_candidates(
    filt: str, new_text: str, new_emb, pool: list[PoolNode]
) -> list[CandidateFact]:
    """Apply a candidate filter. filt is 'full' | 'embed_topk:K' | 'lexical'."""
    if filt == "full":
        return [CandidateFact(id=p.id, text=p.text) for p in pool]
    if filt == "lexical":
        nw = _content_words(new_text)
        keep = [p for p in pool if _content_words(p.text) & nw]
        return [CandidateFact(id=p.id, text=p.text) for p in keep]
    if filt.startswith("embed_topk"):
        k = int(filt.split(":", 1)[1]) if ":" in filt else 10
        scored = [(_cosine(new_emb, p.embedding), p) for p in pool]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [CandidateFact(id=p.id, text=p.text) for _, p in scored[:k]]
    raise ValueError(f"unknown filter {filt!r}")


# ---------------------------------------------------------------------------
# Ingest one case with a given candidate filter. Gold nodes (path B) are the
# graph under test; filler nodes are pool-only distractors (never scored as
# edges, never a valid gold source). Candidate order for node i is the pool of
# everything inserted before it (filler first, then gold in role order) — same
# incremental write-path shape as ingest._discovered_edges, but the LLM sees
# only the filtered shortlist.
# ---------------------------------------------------------------------------


def _gold_specs(case: CascadeCase) -> list[tuple[str, str, str, str]]:
    """(slug, text, role, entity) for root + each target's cur/pre — path B nodes."""
    specs: list[tuple[str, str, str, str]] = []
    root_facts = _gold_facts_for(case, case.root)
    root_fact = _current_value_fact(root_facts, case.root_change.get("after"))
    root_before = (case.entities.get(case.root).before
                   if case.entities.get(case.root) else case.root_change.get("before"))
    root_text = (root_fact or {}).get("fact_text") or f"The {case.root} is {root_before}."
    specs.append((f"root-{case.root}", root_text, "root", case.root))
    seen: set[str] = set()
    for edge in sorted(case.edges, key=lambda e: e.hop):
        target = edge.target
        if target in seen:
            continue
        seen.add(target)
        facts = _gold_facts_for(case, target)
        if not facts:
            continue
        ent = case.entities.get(target)
        after_val = ent.after if ent else None
        cur = _current_value_fact(facts, after_val)
        if cur:
            specs.append((f"cur-{target}", cur.get("fact_text") or cur.get("original_seed") or "", "cur", target))
        pre = _predeclaration_fact(facts)
        if pre:
            specs.append((f"pre-{target}", pre.get("fact_text") or pre.get("original_seed") or "", "pre", target))
    return specs


def _filler_texts(case: CascadeCase, max_nodes: int) -> list[str]:
    """One node per filler session (concatenated user turns), capped at max_nodes."""
    out: list[str] = []
    for sess in case.sessions:
        if sess.get("type") != "filler":
            continue
        turns = [t.get("content", "") for t in sess.get("conversation", []) if t.get("role") == "user"]
        turns = [t for t in turns if t and t.strip()]
        if turns:
            out.append("\n".join(turns))
        if len(out) >= max_nodes:
            break
    return out


async def ingest_with_filter(
    db, case: CascadeCase, account: str, embed_batch,
    discovery: DependencyDiscoveryService, filt: str, max_filler: int,
) -> tuple[IngestedGraph, int]:
    """Ingest gold nodes over a filler-inflated pool, using candidate filter `filt`.

    Returns (graph, avg_candidates_per_new_node). Filler nodes are inserted first
    as pool distractors; then gold nodes arrive one at a time and discover edges
    against the FILTERED shortlist of everything inserted before them.
    """
    graph = IngestedGraph(account_id=account, root_id=None)
    node_by_entity: dict[str, uuid.UUID] = {}

    filler_texts = _filler_texts(case, max_filler)
    gold_specs = _gold_specs(case)

    # One batched embedding call for filler + gold texts together.
    all_texts = filler_texts + [s[1] for s in gold_specs]
    all_emb = await _embed_all(embed_batch, all_texts)
    filler_emb = all_emb[: len(filler_texts)]
    gold_emb = all_emb[len(filler_texts):]

    pool: list[PoolNode] = []

    # Insert filler distractors (pool only; not scored, no gold in-edge).
    for text, emb in zip(filler_texts, filler_emb):
        fid = await _insert_memory(db, account, "filler", text, emb)
        pool.append(PoolNode(id=fid, text=text, embedding=emb))

    # Insert gold nodes incrementally; discover edges against the filtered pool.
    cand_counts: list[int] = []
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

        if pool:  # discover against everything inserted before this gold node
            cands = select_candidates(filt, text, emb, pool)
            cand_counts.append(len(cands))
            if cands:
                source_ids = await discovery.discover_sources(text, cands)
                for src_id in source_ids:
                    await db.execute(
                        """
                        INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
                        VALUES ($1, $2, 'derived_from')
                        ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
                        """,
                        node_id, src_id,
                    )
                    graph.persisted_edges.add((src_id, node_id))
        pool.append(PoolNode(id=node_id, text=text, embedding=emb))

    avg_cands = (sum(cand_counts) / len(cand_counts)) if cand_counts else 0.0
    return graph, avg_cands


@dataclass
class CaseResult:
    episode_id: str
    n_gold: int
    n_pred: int
    n_tp: int
    precision: float
    recall: float
    discovery_tokens: int
    avg_candidates: float


@dataclass
class FilterResult:
    filter: str
    n_cases: int = 0
    n_error: int = 0
    micro_precision: float = 0.0
    micro_recall: float = 0.0
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    total_discovery_tokens: int = 0
    tokens_per_case: float = 0.0
    avg_candidates: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)


async def run_filter(system, prov, model, filt, cases, max_filler) -> FilterResult:
    chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=model)
    )
    discovery = DependencyDiscoveryService(chat)  # naive; filter is the only variable
    embed_batch = system.embedding.embed_batch

    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    res = FilterResult(filter=filt)
    sum_tp = sum_pred = sum_gold = 0
    sum_cands = 0.0
    for i, case in enumerate(cases, 1):
        account = f"cand-{filt[:10]}-{case.episode_id}-{case.target_entity}"[:60]
        try:
            t0 = chat.total_tokens
            async with system.repo.session(account) as db:
                graph, avg_c = await ingest_with_filter(
                    db, case, account, embed_batch, discovery, filt, max_filler
                )
                pr = edge_pr(case, graph)
            dtok = chat.total_tokens - t0
            res.cases.append(CaseResult(
                episode_id=case.episode_id,
                n_gold=pr["n_gold"], n_pred=pr["n_pred"], n_tp=pr["n_tp"],
                precision=pr["precision"], recall=pr["recall"],
                discovery_tokens=dtok, avg_candidates=avg_c,
            ))
            sum_tp += pr["n_tp"]; sum_pred += pr["n_pred"]; sum_gold += pr["n_gold"]
            sum_cands += avg_c
            res.total_discovery_tokens += dtok
            print(f"  [{filt}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"P={pr['precision']:.2f} R={pr['recall']:.2f} "
                  f"cand={avg_c:.0f} tok={dtok}", flush=True)
        except Exception as exc:
            res.n_error += 1
            print(f"  [{filt}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"ERROR {type(exc).__name__}: {exc}", flush=True)

    await chat.close()
    ok = res.cases
    res.n_cases = len(ok)
    res.micro_precision = (sum_tp / sum_pred) if sum_pred else 0.0
    res.micro_recall = (sum_tp / sum_gold) if sum_gold else 0.0
    res.macro_precision = (sum(c.precision for c in ok) / len(ok)) if ok else 0.0
    res.macro_recall = (sum(c.recall for c in ok) / len(ok)) if ok else 0.0
    res.tokens_per_case = (res.total_discovery_tokens / len(ok)) if ok else 0.0
    res.avg_candidates = (sum_cands / len(ok)) if ok else 0.0
    return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None)
    ap.add_argument("--limit", type=int, default=40, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH),
                    help="use meme_filler32k.json so the candidate pool is large")
    ap.add_argument("--provider", default="openlux")
    ap.add_argument("--model", default="gpt-4o-mini",
                    help="discovery model held FIXED across filters")
    ap.add_argument("--max-filler", type=int, default=60,
                    help="cap filler pool nodes per case (keeps full-baseline runnable)")
    ap.add_argument("--filters", nargs="+",
                    default=["full", "embed_topk:5", "embed_topk:10", "embed_topk:20", "lexical"])
    ap.add_argument("--out", default="integrations/memebench/runs/cand_sweep.json")
    args = ap.parse_args()

    system = await build_system(provider_label=args.provider, chat_model=args.model)
    prov = load_provider(args.provider, DEFAULT_PROVIDERS_PATH)
    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.limit:
        cases = cases[: args.limit]

    results: list[FilterResult] = []
    try:
        for filt in args.filters:
            print(f"=== filter: {filt} ({len(cases)} cases, model={args.model}) ===", flush=True)
            results.append(await run_filter(system, prov, args.model, filt, cases, args.max_filler))
    finally:
        await system.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"provider": args.provider, "model": args.model, "hop": args.hop,
         "n_cases": len(cases), "max_filler": args.max_filler,
         "results": [asdict(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"Candidate-selection cost×quality  (hop={args.hop or 'all'}, "
          f"{len(cases)} cases, model={args.model})")
    print("=" * 78)
    print(f"{'filter':<16} {'micro-P':>8} {'micro-R':>8} {'macro-P':>8} "
          f"{'macro-R':>8} {'cand':>6} {'tok/case':>9}")
    for r in results:
        print(f"{r.filter:<16} {r.micro_precision:>8.3f} {r.micro_recall:>8.3f} "
              f"{r.macro_precision:>8.3f} {r.macro_recall:>8.3f} "
              f"{r.avg_candidates:>6.0f} {r.tokens_per_case:>9.0f}"
              + (f"  ({r.n_error} err)" if r.n_error else ""))
    print(f"\nArtifacts -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
