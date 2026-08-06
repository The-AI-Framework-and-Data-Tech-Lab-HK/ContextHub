"""Task ② — edge-discovery cost×quality curve: sweep the discovery MODEL while
holding everything else fixed, on PATH B (gold nodes) so edge P/R is EXACT.

Why path B here (vs path A for the negative-edge set): ② must isolate ONE
variable — the edge-discovery model — and read its quality precisely. Path B
feeds MEME's gold_facts as clean, entity-labelled nodes, so edge_pr compares
persisted edges to gold edges with zero value-mapping noise and zero extraction
noise. The only thing that varies across runs is the discovery model, so the
resulting precision/recall difference is attributable to the model alone. (This
is a controlled internal ablation, NOT feeding gold as a system input to game a
benchmark — the negative-edge eval set stays on path A.)

For each model tier we run naive discovery (edge_mode="discovered", no syntactic
routing — the plain baseline) over a set of cases and record:
  - edge precision / recall (exact, path B)
  - discovery tokens (total + per case), the cost axis

Output: one row per model → the build-side cost×quality curve for edge discovery.

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.discovery_model_sweep \
        --hop 1 --limit 40 --provider openlux \
        --models gpt-4o-mini gpt-4o claude-opus-4-8 \
        --out integrations/memebench/runs/discovery_sweep_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.dependency_discovery_service import (
    DependencyDiscoveryService,
)
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.ingest import edge_pr, ingest_case
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import (
    DEFAULT_PROVIDERS_PATH,
    build_system,
    load_provider,
)


@dataclass
class CaseResult:
    episode_id: str
    n_gold: int
    n_pred: int
    n_tp: int
    precision: float
    recall: float
    discovery_tokens: int


@dataclass
class ModelResult:
    model: str
    n_cases: int = 0
    n_error: int = 0
    # micro (edge-summed) precision/recall + mean discovery tokens
    micro_precision: float = 0.0
    micro_recall: float = 0.0
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    total_discovery_tokens: int = 0
    tokens_per_case: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)


async def run_model(system, prov, model, cases) -> ModelResult:
    """Ingest all cases with naive discovery on `model`; exact edge P/R + tokens."""
    chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=model)
    )
    discovery = DependencyDiscoveryService(chat)  # naive: model is the only variable
    embed_batch = system.embedding.embed_batch

    # Clean slate for this model (per-case accounts isolate, but keep DB tidy).
    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    res = ModelResult(model=model)
    sum_tp = sum_pred = sum_gold = 0
    for i, case in enumerate(cases, 1):
        account = f"disc-{model[:12]}-{case.episode_id}-{case.target_entity}"[:60]
        try:
            t0 = chat.total_tokens
            async with system.repo.session(account) as db:
                graph = await ingest_case(
                    db, case, account, embed_batch,
                    edge_mode="discovered", discovery=discovery,
                )
                pr = edge_pr(case, graph)
            dtok = chat.total_tokens - t0
            res.cases.append(CaseResult(
                episode_id=case.episode_id,
                n_gold=pr["n_gold"], n_pred=pr["n_pred"], n_tp=pr["n_tp"],
                precision=pr["precision"], recall=pr["recall"], discovery_tokens=dtok,
            ))
            sum_tp += pr["n_tp"]; sum_pred += pr["n_pred"]; sum_gold += pr["n_gold"]
            res.total_discovery_tokens += dtok
            print(f"  [{model}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"P={pr['precision']:.2f} R={pr['recall']:.2f} tok={dtok}", flush=True)
        except Exception as exc:
            res.n_error += 1
            print(f"  [{model}] [{i}/{len(cases)}] {case.episode_id}: ERROR {type(exc).__name__}: {exc}", flush=True)

    await chat.close()
    ok = res.cases
    res.n_cases = len(ok)
    res.micro_precision = (sum_tp / sum_pred) if sum_pred else 0.0
    res.micro_recall = (sum_tp / sum_gold) if sum_gold else 0.0
    res.macro_precision = (sum(c.precision for c in ok) / len(ok)) if ok else 0.0
    res.macro_recall = (sum(c.recall for c in ok) / len(ok)) if ok else 0.0
    res.tokens_per_case = (res.total_discovery_tokens / len(ok)) if ok else 0.0
    return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None)
    ap.add_argument("--limit", type=int, default=40, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    ap.add_argument("--provider", default="openlux")
    ap.add_argument("--models", nargs="+",
                    default=["gpt-4o-mini", "gpt-4o", "claude-opus-4-8"])
    ap.add_argument("--out", default="integrations/memebench/runs/discovery_sweep.json")
    args = ap.parse_args()

    system = await build_system(provider_label=args.provider)
    prov = load_provider(args.provider, DEFAULT_PROVIDERS_PATH)
    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.limit:
        cases = cases[: args.limit]

    results: list[ModelResult] = []
    try:
        for model in args.models:
            print(f"=== model: {model} ({len(cases)} cases) ===", flush=True)
            results.append(await run_model(system, prov, model, cases))
    finally:
        await system.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"provider": args.provider, "hop": args.hop, "n_cases": len(cases),
         "results": [asdict(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    print(f"Edge-discovery cost×quality  (hop={args.hop or 'all'}, {len(cases)} cases)")
    print("=" * 68)
    print(f"{'model':<20} {'micro-P':>8} {'micro-R':>8} {'macro-P':>8} {'macro-R':>8} {'tok/case':>9}")
    for r in results:
        print(f"{r.model:<20} {r.micro_precision:>8.3f} {r.micro_recall:>8.3f} "
              f"{r.macro_precision:>8.3f} {r.macro_recall:>8.3f} {r.tokens_per_case:>9.0f}"
              + (f"  ({r.n_error} err)" if r.n_error else ""))
    print(f"\nArtifacts -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
