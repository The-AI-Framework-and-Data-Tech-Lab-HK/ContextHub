"""Stage A: judge-tier calibration on real MEME derived_from edges.

Goal — answer, with data rather than a guess, whether MEME's propagation edges
have any *routing room* for a cost-aware judge optimizer (Problem 2, contribution
2). We take every gold derived_from edge (all of which SHOULD go stale on the
root change) and run four judge tiers on it, recording each tier's verdict vs the
gold "should-stale" label plus its token cost:

  J1  rule       : does the changed root entity string appear in the derived text?
  J2  embedding  : cosine(sim) of (change text, derived text) >= threshold?
  J3  cheap LLM  : DerivedMemoryOracleRule on a cheap model
  J4  costly LLM : DerivedMemoryOracleRule on the default model (current baseline)

Because gold edges are single-polarity (every persisted gold edge is a
should-stale edge — predeclaration nodes get no edge), this run measures RECALL
and cost per tier, NOT precision. If J1/J2/J3 recall collapses and only J4 holds,
MEME has no routing room and we move to a synthetic bed (Stage B). If cheaper
tiers hold on a meaningful fraction of edges, MEME can support part (i).

Read-only w.r.t. the core library: reuses ingest_case(edge_mode="gold") to get
edges + gold labels + node text, and DerivedMemoryOracleRule for the LLM tiers.
Does NOT write change_events or run the propagation engine. Each case uses an
isolated account and is left in the DB (caller truncates between runs).

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.stage_a_calibrate \
        --hop 1 --limit 30 --cheap-model gpt-4o-mini --costly-model gpt-4.1-mini \
        --out integrations/memebench/runs/stage_a_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.propagation.derived_memory_rule import DerivedMemoryOracleRule
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.ingest import ingest_case
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import build_system

EMBED_SIM_THRESHOLDS = (0.30, 0.40, 0.50, 0.60, 0.70)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class EdgeRecord:
    episode_id: str
    hop: int
    change_text: str          # "root: before -> after"
    derived_text: str
    j1_rule_stale: bool       # rule verdict
    j2_cosine: float          # raw cosine (threshold applied at aggregation)
    j3_cheap_stale: bool | None
    j4_costly_stale: bool | None
    j3_tokens: int = 0
    j4_tokens: int = 0
    error: str | None = None


async def _oracle_verdict(rule: DerivedMemoryOracleRule, event: dict[str, Any],
                          dependent_id) -> bool:
    action = await rule.evaluate(event, {"dependent_id": dependent_id})
    return action.action == "mark_stale"


async def calibrate_case(system, case, account, cheap_rule, costly_rule) -> list[EdgeRecord]:
    embed = system.embedding.embed
    embed_batch = system.embedding.embed_batch

    after = case.root_change.get("after")
    before = case.root_change.get("before")
    change_text = f"{case.root}: {before} -> {after}"

    # Phase 1: ingest + collect edge data INSIDE the session. The session commits
    # on block exit; the LLM tiers (phase 2) run afterwards so DerivedMemoryOracle-
    # Rule's own pooled session can see the committed nodes.
    # (tgt_id, src_id, derived_text, hop, j1, cos)
    edges: list[tuple[Any, Any, str, int, bool, float]] = []
    async with system.repo.session(account) as db:
        graph = await ingest_case(db, case, account, embed_batch, edge_mode="gold")
        root_id = graph.root_id
        hop_by_entity = {e.target: e.hop for e in case.edges}
        entity_by_node = {nid: ent for nid, (ent, role) in graph.node_meta.items()}

        async def _text(nid) -> str:
            row = await db.fetchrow(
                "SELECT l2_content, l1_content, l0_content FROM contexts WHERE id = $1", nid
            )
            return (row["l2_content"] or row["l1_content"] or row["l0_content"] or "") if row else ""

        change_emb = await embed(change_text)
        for (src_id, tgt_id) in graph.persisted_edges:
            derived_text = await _text(tgt_id)
            ent = entity_by_node.get(tgt_id, "")
            hop = hop_by_entity.get(ent, 1)
            hay = derived_text.lower()
            needles = [str(case.root).lower(), str(before).lower(), str(after).lower()]
            j1 = any(n and n in hay for n in needles)
            derived_emb = await embed(derived_text)
            j2_cos = _cosine(change_emb, derived_emb) if (change_emb and derived_emb) else 0.0
            edges.append((tgt_id, src_id, derived_text, hop, j1, round(j2_cos, 4)))

    # Phase 2: session committed — now the oracle rules can fetch node content.
    # Per-edge event: if the edge's source is the root, this is a hop-1 edge judged
    # against the root change directly (`modified`). Otherwise the source is an
    # intermediate node that itself just went stale, so we judge the downstream
    # against a `marked_stale` cascade event whose context_id is that intermediate
    # node — mirroring how the engine regenerates events per hop (so the oracle
    # sees "your direct upstream is outdated" rather than the unrelated root).
    def _event_for(src_id):
        if src_id == root_id:
            return {
                "change_type": "modified",
                "account_id": account,
                "context_id": root_id,
                "diff_summary": change_text,
                "metadata": {"before": before, "after": after, "entity": case.root},
            }
        return {
            "change_type": "marked_stale",
            "account_id": account,
            "context_id": src_id,          # the direct (intermediate) upstream node
            "diff_summary": change_text,   # root-change reason, appended by _describe_change
            "metadata": {"before": before, "after": after, "entity": case.root},
        }

    records: list[EdgeRecord] = []
    for (tgt_id, src_id, derived_text, hop, j1, j2_cos) in edges:
        event = _event_for(src_id)
        rec = EdgeRecord(
            episode_id=case.episode_id, hop=hop,
            change_text=change_text, derived_text=derived_text[:200],
            j1_rule_stale=j1, j2_cosine=j2_cos,
            j3_cheap_stale=None, j4_costly_stale=None,
        )
        try:
            c0 = cheap_rule._chat.total_tokens
            rec.j3_cheap_stale = await _oracle_verdict(cheap_rule, event, tgt_id)
            rec.j3_tokens = cheap_rule._chat.total_tokens - c0
            k0 = costly_rule._chat.total_tokens
            rec.j4_costly_stale = await _oracle_verdict(costly_rule, event, tgt_id)
            rec.j4_tokens = costly_rule._chat.total_tokens - k0
        except Exception as exc:  # keep going; record the failure
            rec.error = f"{type(exc).__name__}: {exc}"
        records.append(rec)

    return records


def _recall(flags: list[bool]) -> float | None:
    # Every gold edge SHOULD be stale, so recall = fraction judged stale.
    return (sum(1 for f in flags if f) / len(flags)) if flags else None


def summarize(records: list[EdgeRecord], cheap_model: str, costly_model: str) -> dict:
    ok = [r for r in records if r.error is None]
    n = len(ok)
    j1 = _recall([r.j1_rule_stale for r in ok])
    j3 = _recall([bool(r.j3_cheap_stale) for r in ok])
    j4 = _recall([bool(r.j4_costly_stale) for r in ok])
    # J2 recall at each threshold: cosine >= t counts as "predict stale".
    j2 = {f"thr_{t}": _recall([r.j2_cosine >= t for r in ok]) for t in EMBED_SIM_THRESHOLDS}
    cos_vals = sorted(r.j2_cosine for r in ok)
    return {
        "n_edges": n,
        "n_error": len(records) - n,
        "cheap_model": cheap_model,
        "costly_model": costly_model,
        "note": "gold edges are all should-stale => this is RECALL + cost, NOT precision.",
        "recall": {
            "J1_rule": j1,
            "J2_embedding_by_threshold": j2,
            "J3_cheap_llm": j3,
            "J4_costly_llm": j4,
        },
        "cost_tokens_total": {
            "J3_cheap_llm": sum(r.j3_tokens for r in ok),
            "J4_costly_llm": sum(r.j4_tokens for r in ok),
        },
        "cost_tokens_per_edge": {
            "J3_cheap_llm": (sum(r.j3_tokens for r in ok) / n) if n else None,
            "J4_costly_llm": (sum(r.j4_tokens for r in ok) / n) if n else None,
        },
        "cosine_distribution": {
            "min": cos_vals[0] if cos_vals else None,
            "p50": cos_vals[len(cos_vals) // 2] if cos_vals else None,
            "max": cos_vals[-1] if cos_vals else None,
        },
        "by_hop": {
            str(h): {
                "n": len([r for r in ok if r.hop == h]),
                "J1_rule": _recall([r.j1_rule_stale for r in ok if r.hop == h]),
                "J3_cheap_llm": _recall([bool(r.j3_cheap_stale) for r in ok if r.hop == h]),
                "J4_costly_llm": _recall([bool(r.j4_costly_stale) for r in ok if r.hop == h]),
            }
            for h in sorted({r.hop for r in ok})
        },
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=1)
    ap.add_argument("--limit", type=int, default=30, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    ap.add_argument("--cheap-model", default="gpt-4o-mini")
    ap.add_argument("--costly-model", default="gpt-4.1-mini")
    ap.add_argument("--out", default="integrations/memebench/runs/stage_a.json")
    args = ap.parse_args()

    system = await build_system(chat_model=args.costly_model)
    # Two independent counting chat clients (distinct models) so tokens attribute
    # per tier; both oracle rules share the system repo for RLS content fetch.
    from integrations.memebench.systems import load_provider, DEFAULT_PROVIDERS_PATH
    prov = load_provider("yunwu", DEFAULT_PROVIDERS_PATH)
    cheap_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=args.cheap_model)
    )
    costly_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=args.costly_model)
    )
    cheap_rule = DerivedMemoryOracleRule(cheap_chat, system.repo)
    costly_rule = DerivedMemoryOracleRule(costly_chat, system.repo)

    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.limit:
        cases = cases[: args.limit]

    all_records: list[EdgeRecord] = []
    try:
        for i, case in enumerate(cases, 1):
            account = f"stageA-{case.episode_id}-{case.target_entity}"[:60]
            try:
                recs = await calibrate_case(system, case, account, cheap_rule, costly_rule)
                all_records.extend(recs)
                print(f"[{i}/{len(cases)}] {case.episode_id}/{case.target_entity}: "
                      f"{len(recs)} edges", flush=True)
            except Exception as exc:
                print(f"[{i}/{len(cases)}] {case.episode_id}: ERROR {exc}", flush=True)
    finally:
        await cheap_chat.close()
        await costly_chat.close()
        await system.close()

    summary = summarize(all_records, args.cheap_model, args.costly_model)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict
    out.write_text(json.dumps(
        {"summary": summary, "edges": [asdict(r) for r in all_records]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"Stage A calibration  (hop={args.hop}, {summary['n_edges']} edges)")
    print("=" * 60)
    r = summary["recall"]
    print(f"J1 rule recall      : {r['J1_rule']}")
    print(f"J2 embedding recall : {r['J2_embedding_by_threshold']}")
    print(f"J3 cheap LLM recall : {r['J3_cheap_llm']}  ({args.cheap_model})")
    print(f"J4 costly LLM recall: {r['J4_costly_llm']}  ({args.costly_model})")
    print(f"cosine dist         : {summary['cosine_distribution']}")
    print(f"tokens/edge J3/J4   : {summary['cost_tokens_per_edge']}")
    print(f"\nArtifacts written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
