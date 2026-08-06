"""Generate the bipolar (should-stale / should-NOT-stale) edge evaluation set for
Problem 2, built from PATH A (real extraction from raw dialogue + naive edge
discovery) — NOT from MEME's gold_facts.

Why path A: we deliberately let the extractor produce noisy, chatter-laden nodes
(pottery, "I drive a Xylorim Scooter", partner James, ...) so that naive
discovery makes the kind of SEMANTIC mislinks a real system makes. Those mislinks
onto off-topic nodes are pure-semantic negative edges (no "if..." signature, a
free regex cannot catch them) — the exact traps Problem 2's judge must resist.
Path B (gold_facts) can never produce them because it has no chatter nodes.

The price: path-A nodes carry no entity label, so each node's true subject is
recovered by value-string mapping (reusing the entities_of rule edge_pr_raw
already uses). That mapping is noisy, so the auto-labels are APPROXIMATE. This
script therefore also emits a human-check sample and expects the caller to report
a label error rate before the set is trusted (option 2: auto-label + sampled
human audit).

Label rule — hangs on "does the DOWNSTREAM (dependent) node itself truly go
stale under this root change", NOT on "is this edge correct":

  1. dependent looks like a predeclaration (leading "if ... will/would ...")
        -> should_stale = False   (negative, syntactically catchable)
  2. else dependent maps (by value) to any cascade-TARGET entity
        -> should_stale = True    (positive edge)
  3. else (maps only to non-target/chatter entities, or to nothing)
        -> should_stale = False   (negative, PURE SEMANTIC — the valuable trap)

`regex_fires` (does _CONDITIONAL_RE match the dependent text) is recorded
separately so Problem 2 can measure "what fraction of negatives a free regex
alone would kill" vs "what needs a paid semantic judge".

Read-only w.r.t. the core library: reuses ingest_case_raw (path A) + the naive
DependencyDiscoveryService, exactly as run_eval.py's raw path does. Does not run
the propagation engine (the should_stale label is derived from gold cascade
targets, not from a live cascade).

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.build_negative_edge_set \
        --hop 1 --limit 20 \
        --out integrations/memebench/runs/neg_edge_set_hop1.json \
        --sample-out integrations/memebench/runs/neg_edge_sample_hop1.json --sample 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import re

from contexthub.services.dependency_discovery_service import (
    DependencyDiscoveryService,
)
from integrations.memebench.ingest import ingest_case_raw
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    CascadeCase,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import build_system


def _norm(s: Any) -> str:
    return " ".join(str(s or "").casefold().split())


def _stem(w: str) -> str:
    """Strip common inflectional suffixes so 'intolerance' and 'intolerant' unify."""
    for suf in ("ance", "ancy", "ant", "ations", "ation", "ings", "ing", "edly", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _stem_tokens(text: str) -> set[str]:
    return {_stem(t) for t in re.findall(r"[a-z0-9]+", _norm(text))}


@dataclass
class LabeledEdge:
    episode_id: str
    dependent_id: str
    dependency_id: str
    dependent_text: str
    dependency_text: str
    dependent_entities: list[str]     # entities the downstream node maps to (by value)
    should_stale: bool                # the label Problem 2 scores against
    polarity: str                     # positive / negative
    neg_class: str                    # "" / predeclaration / semantic_offtopic
    regex_fires: bool                 # would the free _CONDITIONAL_RE catch it?


_STOP = {"a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "and", "or",
         "is", "are", "my", "their", "his", "her", "with", "per", "week", "weekly"}


def _entities_of(text: str, case: CascadeCase) -> set[str]:
    """Map a node's text to gold entities by before-value match.

    Two tiers: (1) exact substring (original edge_pr_raw rule), then (2) a stemmed
    content-token match — every content token of the before value (stemmed) must
    appear in the node's stemmed token set. Tier 2 recovers inflectional variants
    like 'lactose intolerance' (value) vs 'lactose intolerant' (node text), which
    exact substring misses; requiring ALL content tokens avoids single-word noise.
    """
    tn = _norm(text)
    node_stems = _stem_tokens(text)
    out: set[str] = set()
    for name, ent in case.entities.items():
        bv = _norm(ent.before)
        if not bv:
            continue
        if bv in tn:
            out.add(name)
            continue
        content = [t for t in re.findall(r"[a-z0-9]+", bv) if t not in _STOP and len(t) > 2]
        if content and all(_stem(t) in node_stems for t in content):
            out.add(name)
    return out


def _label_edge(
    dependent_text: str, dependent_ents: set[str], cascade_targets: set[str], root: str
) -> tuple[bool, str, str]:
    """Return (should_stale, polarity, neg_class) for one edge's downstream node.

    should_stale is True when the downstream node itself becomes outdated under the
    root change: either it maps to a cascade-target entity, OR it restates the root
    entity itself (which is the thing being changed). Predeclarations become correct
    after the change and must NOT go stale.
    """
    looks_cond = DependencyDiscoveryService._looks_conditional(dependent_text)
    if looks_cond:
        # Predeclaration rule: becomes correct after the change, must NOT go stale.
        return False, "negative", "predeclaration"
    # A node that restates the changed root entity is itself outdated.
    if root in dependent_ents:
        return True, "positive", ""
    hit_targets = dependent_ents & cascade_targets
    if hit_targets:
        return True, "positive", ""
    # Maps only to non-target/chatter entities (or nothing): should NOT go stale.
    return False, "negative", "semantic_offtopic"


async def label_case(system, case: CascadeCase, account: str) -> tuple[list[LabeledEdge], int]:
    """Path-A ingest one case, then auto-label every persisted edge. Returns
    (labeled_edges, n_extracted_nodes)."""
    embed_batch = system.embedding.embed_batch
    cascade_targets = {e.target for e in case.edges}

    async with system.repo.session(account) as db:
        # Path A: real extraction + naive discovery (system.discovery = naive).
        graph = await ingest_case_raw(
            db, case, account, embed_batch, system.extractor, system.discovery,
        )

        # node text cache from inserted_nodes (path A populates it in order)
        text_by_id = {nid: t for nid, t in graph.inserted_nodes}
        ent_by_id = {nid: _entities_of(t, case) for nid, t in graph.inserted_nodes}

        labeled: list[LabeledEdge] = []
        # persisted_edges stored as (dependency, dependent) == (src, tgt).
        for (dep_id, dependent_id) in graph.persisted_edges:
            dtext = text_by_id.get(dependent_id, "")
            utext = text_by_id.get(dep_id, "")
            dents = ent_by_id.get(dependent_id, set())
            should_stale, polarity, neg_class = _label_edge(
                dtext, dents, cascade_targets, case.root
            )
            labeled.append(LabeledEdge(
                episode_id=case.episode_id,
                dependent_id=str(dependent_id),
                dependency_id=str(dep_id),
                dependent_text=dtext[:220],
                dependency_text=utext[:220],
                dependent_entities=sorted(dents),
                should_stale=should_stale,
                polarity=polarity,
                neg_class=neg_class,
                regex_fires=DependencyDiscoveryService._looks_conditional(dtext),
            ))
        return labeled, len(graph.inserted_nodes)


def summarize(edges: list[LabeledEdge], n_nodes: int, n_cases: int) -> dict:
    pol = Counter(e.polarity for e in edges)
    negs = [e for e in edges if e.polarity == "negative"]
    neg_cls = Counter(e.neg_class for e in negs)
    # Of the negatives, how many would a free regex alone catch vs need a judge?
    neg_regex_caught = sum(1 for e in negs if e.regex_fires)
    neg_semantic = sum(1 for e in negs if not e.regex_fires)
    return {
        "n_cases": n_cases,
        "n_extracted_nodes": n_nodes,
        "n_edges": len(edges),
        "polarity": dict(pol),
        "negative_class": dict(neg_cls),
        "negatives_free_regex_caught": neg_regex_caught,
        "negatives_needing_semantic_judge": neg_semantic,
        "frac_negatives_semantic": (neg_semantic / len(negs)) if negs else None,
        "note": (
            "Path-A auto-labels via value mapping are APPROXIMATE; run the "
            "human-check sample and report a label error rate before trusting."
        ),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None)
    ap.add_argument("--limit", type=int, default=20, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    ap.add_argument("--out", default="integrations/memebench/runs/neg_edge_set.json")
    ap.add_argument("--sample-out", default="integrations/memebench/runs/neg_edge_sample.json")
    ap.add_argument("--sample", type=int, default=60, help="edges to emit for human check")
    ap.add_argument("--provider", default="yunwu", help="chat/oracle provider label")
    ap.add_argument("--episodes", nargs="*", default=None,
                    help="only build these episode_ids (for refilling failed cases)")
    args = ap.parse_args()

    system = await build_system(provider_label=args.provider)
    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.episodes:
        want = set(args.episodes)
        cases = [c for c in cases if c.episode_id in want]
    elif args.limit:
        cases = cases[: args.limit]

    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    all_edges: list[LabeledEdge] = []
    n_nodes_total = 0
    n_ok_cases = 0
    try:
        for i, case in enumerate(cases, 1):
            account = f"neg-{case.episode_id}-{case.target_entity}"[:60]
            try:
                edges, n_nodes = await label_case(system, case, account)
                all_edges.extend(edges)
                n_nodes_total += n_nodes
                n_ok_cases += 1
                npos = sum(1 for e in edges if e.polarity == "positive")
                nneg = len(edges) - npos
                print(f"[{i}/{len(cases)}] {case.episode_id}/{case.target_entity}: "
                      f"nodes={n_nodes} edges={len(edges)} pos={npos} neg={nneg}", flush=True)
            except Exception as exc:
                print(f"[{i}/{len(cases)}] {case.episode_id}: ERROR {type(exc).__name__}: {exc}", flush=True)
    finally:
        await system.close()

    summary = summarize(all_edges, n_nodes_total, n_ok_cases)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"summary": summary, "edges": [asdict(e) for e in all_edges]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    # Human-check sample: stratify so negatives (the risky labels) are covered.
    # Deterministic pick (no RNG): interleave positives and each negative class.
    negs = [e for e in all_edges if e.polarity == "negative"]
    pos = [e for e in all_edges if e.polarity == "positive"]
    sem = [e for e in negs if e.neg_class == "semantic_offtopic"]
    pre = [e for e in negs if e.neg_class == "predeclaration"]
    k = args.sample
    # weight toward semantic negatives (the labels most likely to be wrong)
    picks = (sem[: k // 2] + pre[: k // 4] + pos[: k - (k // 2) - (k // 4)])
    sample_out = Path(args.sample_out)
    sample_out.write_text(json.dumps(
        {"instructions": (
            "For each edge, verify the auto label. Check `should_stale`: does the "
            "DEPENDENT node truly become outdated when the root changes? Set "
            "`human_should_stale` to true/false and leave `human_note` if the auto "
            "label is wrong. Then we compute the label error rate."
        ),
         "edges": [{**asdict(e), "human_should_stale": None, "human_note": ""} for e in picks]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 64)
    print(f"Negative-edge set  (hop={args.hop or 'all'}, {n_ok_cases} cases)")
    print("=" * 64)
    print(f"nodes={summary['n_extracted_nodes']}  edges={summary['n_edges']}")
    print(f"polarity            : {summary['polarity']}")
    print(f"negative class      : {summary['negative_class']}")
    print(f"neg free-regex/judge: {summary['negatives_free_regex_caught']} / {summary['negatives_needing_semantic_judge']}")
    print(f"frac neg semantic   : {summary['frac_negatives_semantic']}")
    print(f"\nEval set  -> {out}")
    print(f"Sample    -> {sample_out}  ({len(picks)} edges for human check)")


if __name__ == "__main__":
    asyncio.run(main())
