"""MEME Cascade external-control eval: paired OFF/ON over cascade cases.

Design (per plan):
- Internal ablation: same ContextHub, single variable = failure-propagation layer
  ON vs OFF. Retrieval/answer/judge held constant.
- Paired within-case: ingest once -> before-Q -> apply root_change -> measure OFF
  (before drain) -> drain -> measure ON. This ordering requires the root_change
  event to exist but not yet be drained when OFF is measured, so cases run
  SERIALLY (a global concurrent engine would drain a case's event early and
  break the OFF-before-drain invariant). LLM latency dominates anyway.
- Isolation: a unique account_id per case (RLS scopes contexts/retrieval), so no
  truncate-between-cases races and per-case state is inspectable.

Metrics: accuracy (trivial-pass + raw after) by arm and by hop; oracle/answer
call counts + est. tokens.

Run: CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.run_eval \
        [--data PATH] [--limit N] [--hop {1,2}] [--chat-model M] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from integrations.memebench.answer import answer_question
from integrations.memebench.ingest import (
    apply_root_change,
    apply_root_change_raw,
    edge_pr,
    edge_pr_raw,
    ingest_case,
    ingest_case_raw,
    ingest_filler,
)
from integrations.memebench.judge import CaseVerdict, judge_case_async
from integrations.memebench.loader import CascadeCase, extract_cascade_cases, load_episodes
from integrations.memebench.systems import EvalSystem, build_system

DEFAULT_DATA = "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"


@dataclass
class CaseResult:
    episode_id: str
    domain: str
    hop: int
    target_entity: str
    gold_answer: str
    before_answer: str
    off_answer: str
    on_answer: str
    before_ok: bool
    off_after_ok: bool
    on_after_ok: bool
    off_trivial_pass: bool
    on_trivial_pass: bool
    oracle_calls: int
    n_filler: int
    edge_mode: str = "gold"
    # edge discovery P/R vs gold (only meaningful for edge_mode="discovered")
    edge_n_gold: int = 0
    edge_n_pred: int = 0
    edge_n_tp: int = 0
    edge_precision: float | None = None
    edge_recall: float | None = None
    # raw-dialogue (mode B) only: how many stored nodes the change detector
    # flagged as superseded (0 => no cascade could fire this case).
    raw_dialogue: bool = False
    n_superseded: int | None = None
    error: str | None = None


def _account_for(case: CascadeCase) -> str:
    return f"meme-{case.episode_id}-{case.target_entity}"[:60]


async def run_one(
    system: EvalSystem, case: CascadeCase, *, filler_granularity: str, edge_mode: str,
    llm_judge: bool = False, raw_dialogue: bool = False,
) -> CaseResult:
    account = _account_for(case)
    embed = system.embedding.embed
    embed_batch = system.embedding.embed_batch
    oracle_calls_before = system.oracle_chat.call_count
    n_superseded: int | None = None

    try:
        discovery_svc = {
            "discovered": system.discovery,
            "discovered_tiered": system.discovery_tiered,
            "discovered_hard": system.discovery_hard,
        }.get(edge_mode)
        async with system.repo.session(account) as db:
            if raw_dialogue:
                graph = await ingest_case_raw(
                    db, case, account, embed_batch, system.extractor, discovery_svc,
                )
                pr = edge_pr_raw(case, graph)
            else:
                graph = await ingest_case(
                    db, case, account, embed_batch,
                    edge_mode=edge_mode,
                    discovery=discovery_svc,
                )
                pr = edge_pr(case, graph)
            n_filler = await ingest_filler(db, case, account, embed_batch, granularity=filler_granularity)

        before_answer = ""
        before_gold = case.before_question.expected_answer if case.before_question else None
        if case.before_question:
            async with system.repo.session(account) as db:
                before_answer = (await answer_question(system, db, account, case.before_question.question)).answer

        async with system.repo.session(account) as db:
            if raw_dialogue:
                superseded = await apply_root_change_raw(
                    db, case, account, graph, embed_batch,
                    system.extractor, system.discovery_chat,
                )
                n_superseded = len(superseded)
            else:
                await apply_root_change(db, case, graph, embed)

        # OFF: event exists, not yet drained.
        async with system.repo.session(account) as db:
            off_answer = (await answer_question(system, db, account, case.after_question.question)).answer

        # ON: drain (this case's events; serial run => no other case's events pending).
        engine = system.build_engine(cascade_on_stale=True)
        engine._running = True
        for _ in range(8):
            await engine._drain_ready_events(context_id=None)

        async with system.repo.session(account) as db:
            on_answer = (await answer_question(system, db, account, case.after_question.question)).answer

        judge_chat = system.judge_chat if llm_judge else None
        bq = case.before_question.question if case.before_question else ""
        aq = case.after_question.question
        off_v: CaseVerdict = await judge_case_async(
            before_answer, before_gold, off_answer, case.after_question.expected_answer,
            before_question=bq, after_question=aq, chat=judge_chat,
        )
        on_v: CaseVerdict = await judge_case_async(
            before_answer, before_gold, on_answer, case.after_question.expected_answer,
            before_question=bq, after_question=aq, chat=judge_chat,
        )

        return CaseResult(
            episode_id=case.episode_id, domain=case.domain, hop=case.hop,
            target_entity=case.target_entity, gold_answer=case.gold_answer,
            before_answer=before_answer, off_answer=off_answer, on_answer=on_answer,
            before_ok=on_v.before_ok,
            off_after_ok=off_v.after_ok, on_after_ok=on_v.after_ok,
            off_trivial_pass=off_v.trivial_pass, on_trivial_pass=on_v.trivial_pass,
            oracle_calls=system.oracle_chat.call_count - oracle_calls_before, n_filler=n_filler,
            edge_mode=edge_mode,
            edge_n_gold=int(pr["n_gold"]), edge_n_pred=int(pr["n_pred"]), edge_n_tp=int(pr["n_tp"]),
            edge_precision=float(pr["precision"]), edge_recall=float(pr["recall"]),
            raw_dialogue=raw_dialogue, n_superseded=n_superseded,
        )
    except Exception as exc:  # fail-soft per case
        return CaseResult(
            episode_id=case.episode_id, domain=case.domain, hop=case.hop,
            target_entity=case.target_entity, gold_answer=case.gold_answer,
            before_answer="", off_answer="", on_answer="",
            before_ok=False, off_after_ok=False, on_after_ok=False,
            off_trivial_pass=False, on_trivial_pass=False,
            oracle_calls=0, n_filler=0, edge_mode=edge_mode,
            error=f"{type(exc).__name__}: {exc}",
        )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--hop", type=int, choices=[1, 2], default=None)
    ap.add_argument("--chat-model", default="gpt-4o-mini")
    ap.add_argument("--filler-granularity", default="session", choices=["session", "turn"])
    ap.add_argument("--edge-mode", default="gold",
                    choices=["gold", "discovered", "discovered_tiered", "discovered_hard"],
                    help="gold=MEME dependency_edges_used (oracle upper bound); "
                         "discovered=naive LLM discovery; "
                         "discovered_tiered=conditional-aware syntactic routing + LLM; "
                         "discovered_hard=syntactic hard-exclude of conditional facts")
    ap.add_argument("--case-timeout", type=float, default=120.0,
                    help="per-case wall-clock timeout in seconds (stuck call -> error, skip)")
    ap.add_argument("--llm-judge", action="store_true",
                    help="grade answers with an LLM judge (MEME §4.1 parity) instead of "
                         "string containment; uses system.judge_chat")
    ap.add_argument("--judge-model", default="gpt-4o",
                    help="model for the LLM judge (MEME uses GPT-4o); only used with --llm-judge")
    ap.add_argument("--raw-dialogue", action="store_true",
                    help="mode B: ingest from raw evidence dialogue (per-session extraction, "
                         "no gold_facts / no entity schema); root change is detected zero-oracle. "
                         "Requires --edge-mode discovered*.")
    ap.add_argument("--extract-model", default="claude-opus-4-8",
                    help="extractor model for --raw-dialogue (mode B)")
    ap.add_argument("--sample", type=int, default=None,
                    help="randomly sample this many cases (reproducible via --seed) "
                         "instead of taking the first --limit")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --sample")
    ap.add_argument("--out", default="integrations/memebench/runs")
    args = ap.parse_args()

    if args.raw_dialogue and args.edge_mode == "gold":
        ap.error("--raw-dialogue has no entity labels at insert time; use "
                 "--edge-mode discovered / discovered_tiered / discovered_hard")

    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.sample:
        import random
        cases = random.Random(args.seed).sample(cases, min(args.sample, len(cases)))
    elif args.limit:
        cases = cases[: args.limit]
    print(f"Running {len(cases)} cascade cases (hop={args.hop or 'all'}) with {args.chat_model} "
          f"[edge_mode={args.edge_mode}{' raw-dialogue extractor=' + args.extract_model if args.raw_dialogue else ''}]",
          flush=True)

    system = await build_system(
        chat_model=args.chat_model, judge_model=args.judge_model, extract_model=args.extract_model,
    )
    # start clean
    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    results: list[CaseResult] = []
    try:
        for i, case in enumerate(cases, 1):
            try:
                r = await asyncio.wait_for(
                    run_one(system, case, filler_granularity=args.filler_granularity,
                            edge_mode=args.edge_mode, llm_judge=args.llm_judge,
                            raw_dialogue=args.raw_dialogue),
                    timeout=args.case_timeout,
                )
            except asyncio.TimeoutError:
                r = CaseResult(
                    episode_id=case.episode_id, domain=case.domain, hop=case.hop,
                    target_entity=case.target_entity, gold_answer=case.gold_answer,
                    before_answer="", off_answer="", on_answer="",
                    before_ok=False, off_after_ok=False, on_after_ok=False,
                    off_trivial_pass=False, on_trivial_pass=False,
                    oracle_calls=0, n_filler=0, edge_mode=args.edge_mode,
                    raw_dialogue=args.raw_dialogue,
                    error=f"case_timeout>{args.case_timeout}s",
                )
            results.append(r)
            flag = "ERR" if r.error else ("OK" if (not r.off_after_ok and r.on_after_ok) else "..")
            print(f"  [{i}/{len(cases)}] {flag} {case.episode_id}/{case.target_entity} "
                  f"hop{r.hop} OFF={r.off_after_ok} ON={r.on_after_ok} "
                  f"{'<'+r.error+'>' if r.error else ''}", flush=True)
    finally:
        answer_snap = system.answer_chat.snapshot()
        oracle_snap = system.oracle_chat.snapshot()
        discovery_snap = system.discovery_chat.snapshot()
        extract_snap = system.extract_chat.snapshot()
        judge_snap = system.judge_chat.snapshot()
        await system.close()

    _report(results, args, answer_snap, oracle_snap, discovery_snap,
            extract_snap, judge_snap)


def _report(results, args, answer_snap, oracle_snap, discovery_snap,
            extract_snap, judge_snap):
    from integrations.memebench.metrics import summarize, print_summary, write_artifacts
    summary = summarize(results, answer_snap, oracle_snap, discovery_snap,
                        model=args.chat_model, edge_mode=args.edge_mode,
                        extract_snap=extract_snap, judge_snap=judge_snap)
    print_summary(summary)
    out_dir = Path(args.out)
    write_artifacts(out_dir, results, summary)
    print(f"\nArtifacts written to {out_dir}/")


if __name__ == "__main__":
    asyncio.run(main())
