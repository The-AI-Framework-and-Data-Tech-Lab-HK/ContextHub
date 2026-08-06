"""Diagnostic: classify the FALSE derived_from edges naive discovery makes on MEME.

Go/no-go for the "build-graph produces the negative-edge bed" plan (Problem 1 ->
Problem 2). Task 2 needs a bed with NEGATIVE edges (edges that look derived but
should NOT go stale) to measure judge precision. Naive discovery over-links, so
its false edges ARE those negative edges — produced for free by the build path.

The risk we are testing (Tension 1): if EVERY false edge is a predeclaration
mislink (a leading "if ... will/would" fact, killable by the zero-cost
_CONDITIONAL_RE that conditional_hard already uses), then measuring precision on
this bed degenerates to "one free regex solves it" — exactly the collapse Stage A
hit on the recall side. The bed is only useful if a meaningful fraction of false
edges are SEMANTIC mislinks with no syntactic signature — avoidable only by a
paid LLM judge.

So for every FALSE edge we ask the decisive question: does the free
_CONDITIONAL_RE fire on the edge's DEPENDENT (downstream) node text?

  syntactic_killable : yes -> conditional_hard removes it at zero cost
  semantic_only      : no  -> only a paid judge could have avoided this mislink

and cross-classify by the dependent node's known role (pre / cur / root), which
is exact in gold-node discovered mode (node_meta carries the role). "cur"-target
false edges are wrong-source mislinks onto a materialized node: no syntactic
signature, the interesting class.

Read-only w.r.t. the core library: reuses ingest_case(edge_mode="discovered")
with the naive discovery service — the exact build path run_eval.py uses. Does
not write change_events or run the propagation engine.

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.false_edge_taxonomy \
        --hop 1 --limit 30 \
        --out integrations/memebench/runs/false_edge_taxonomy_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from contexthub.services.dependency_discovery_service import (
    DependencyDiscoveryService,
)
from integrations.memebench.ingest import ingest_case
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import build_system


@dataclass
class FalseEdge:
    episode_id: str
    hop: int
    dependent_role: str        # role of downstream node: pier: cur / pre / root
    dependency_role: str       # role of upstream node
    regex_fires: bool          # does _CONDITIONAL_RE match the dependent text?
    edge_class: str            # predecl_mislink / wrong_source_onto_materialized / onto_root / other
    killable: str              # syntactic_killable / semantic_only
    dependent_text: str
    dependency_text: str


async def classify_case(system, case, account) -> tuple[list[FalseEdge], int, int]:
    """Ingest one case with naive discovery; return (false_edges, n_gold, n_persisted)."""
    embed_batch = system.embedding.embed_batch

    async with system.repo.session(account) as db:
        graph = await ingest_case(
            db, case, account, embed_batch,
            edge_mode="discovered", discovery=system.discovery,
        )

        async def _text(nid) -> str:
            row = await db.fetchrow(
                "SELECT l2_content, l1_content, l0_content FROM contexts WHERE id = $1", nid
            )
            return (row["l2_content"] or row["l1_content"] or row["l0_content"] or "") if row else ""

        # Gold edge set, exactly as edge_pr computes it: (dependency, dependent)
        # = (source-entity node, materialized target node).
        node_by_entity: dict[str, Any] = {}
        if graph.root_id is not None:
            node_by_entity[case.root] = graph.root_id
        node_by_entity.update(graph.materialized)
        gold: set[tuple[Any, Any]] = set()
        for e in case.edges:
            src = node_by_entity.get(e.source)
            tgt = graph.materialized.get(e.target)
            if src is not None and tgt is not None:
                gold.add((src, tgt))

        hop_by_entity = {e.target: e.hop for e in case.edges}
        false_edges: list[FalseEdge] = []
        # persisted_edges are stored as (dependency, dependent) == (src, tgt).
        for (dep_id, dependent_id) in graph.persisted_edges:
            if (dep_id, dependent_id) in gold:
                continue  # true edge
            dependent_ent, dependent_role = graph.node_meta.get(dependent_id, ("", "?"))
            _, dependency_role = graph.node_meta.get(dep_id, ("", "?"))
            dtext = await _text(dependent_id)
            utext = await _text(dep_id)
            regex_fires = DependencyDiscoveryService._looks_conditional(dtext)

            if dependent_role == "pre":
                edge_class = "predecl_mislink"
            elif dependent_role == "cur":
                edge_class = "wrong_source_onto_materialized"
            elif dependent_role == "root":
                edge_class = "onto_root"
            else:
                edge_class = "other"

            false_edges.append(FalseEdge(
                episode_id=case.episode_id,
                hop=hop_by_entity.get(dependent_ent, case.hop),
                dependent_role=dependent_role,
                dependency_role=dependency_role,
                regex_fires=regex_fires,
                edge_class=edge_class,
                killable="syntactic_killable" if regex_fires else "semantic_only",
                dependent_text=dtext[:200],
                dependency_text=utext[:200],
            ))

        return false_edges, len(gold), len(graph.persisted_edges)


def summarize(false_edges: list[FalseEdge], n_gold_total: int, n_persisted_total: int) -> dict:
    n_false = len(false_edges)
    killable = Counter(fe.killable for fe in false_edges)
    by_class = Counter(fe.edge_class for fe in false_edges)
    # The decisive number: of false edges, how many need a paid judge?
    semantic_only = killable.get("semantic_only", 0)
    frac_semantic = (semantic_only / n_false) if n_false else None
    return {
        "n_gold_edges": n_gold_total,
        "n_persisted_edges": n_persisted_total,
        "n_true_edges": n_persisted_total - n_false,
        "n_false_edges": n_false,
        "false_edge_precision_loss": (n_false / n_persisted_total) if n_persisted_total else None,
        "killable_split": dict(killable),
        "frac_false_needing_paid_judge": frac_semantic,
        "by_edge_class": dict(by_class),
        "by_hop": {
            str(h): {
                "n_false": len([fe for fe in false_edges if fe.hop == h]),
                "semantic_only": len([fe for fe in false_edges if fe.hop == h and fe.killable == "semantic_only"]),
                "syntactic_killable": len([fe for fe in false_edges if fe.hop == h and fe.killable == "syntactic_killable"]),
            }
            for h in sorted({fe.hop for fe in false_edges})
        },
        "go_no_go": (
            "NO DATA (no false edges)" if n_false == 0 else
            "VIABLE: meaningful semantic-only fraction" if (frac_semantic or 0) >= 0.2 else
            "THIN: false edges mostly syntactic-killable -> bed degenerates like Stage A"
        ),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None, help="filter by hop (default: all)")
    ap.add_argument("--limit", type=int, default=30, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    ap.add_argument("--out", default="integrations/memebench/runs/false_edge_taxonomy.json")
    args = ap.parse_args()

    system = await build_system()
    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.limit:
        cases = cases[: args.limit]

    # Start clean.
    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    all_false: list[FalseEdge] = []
    n_gold_total = 0
    n_persisted_total = 0
    try:
        for i, case in enumerate(cases, 1):
            account = f"fet-{case.episode_id}-{case.target_entity}"[:60]
            try:
                fes, n_gold, n_persisted = await classify_case(system, case, account)
                all_false.extend(fes)
                n_gold_total += n_gold
                n_persisted_total += n_persisted
                print(f"[{i}/{len(cases)}] {case.episode_id}/{case.target_entity}: "
                      f"persisted={n_persisted} gold={n_gold} false={len(fes)}", flush=True)
            except Exception as exc:
                print(f"[{i}/{len(cases)}] {case.episode_id}: ERROR {exc}", flush=True)
    finally:
        await system.close()

    summary = summarize(all_false, n_gold_total, n_persisted_total)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"summary": summary, "false_edges": [asdict(fe) for fe in all_false]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 64)
    print(f"False-edge taxonomy  (hop={args.hop or 'all'}, {len(cases)} cases)")
    print("=" * 64)
    print(f"persisted={summary['n_persisted_edges']}  gold={summary['n_gold_edges']}  "
          f"true={summary['n_true_edges']}  false={summary['n_false_edges']}")
    print(f"killable split      : {summary['killable_split']}")
    print(f"by edge class       : {summary['by_edge_class']}")
    print(f"frac needing judge  : {summary['frac_false_needing_paid_judge']}")
    print(f"by hop              : {summary['by_hop']}")
    print(f"\n>>> GO/NO-GO: {summary['go_no_go']}")
    print(f"\nArtifacts written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
