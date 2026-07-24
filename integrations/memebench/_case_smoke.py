"""Single-case go/no-go smoke, paired OFF/ON. Works for 1-hop and 2-hop.

Run: CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench._case_smoke [episode_id]
Default episode sw_033 (1-hop). Pass e.g. pl_001 for a 2-hop case.
Requires PG on localhost:5432 and yunwu keys in model_providers.local.json.
"""

from __future__ import annotations

import asyncio
import sys

from integrations.memebench.answer import answer_question
from integrations.memebench.ingest import apply_root_change, ingest_case, ingest_filler
from integrations.memebench.judge import judge_case
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.systems import build_system

ACCOUNT = "meme-eval"
DATA_PATH = "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"


async def _truncate(system):
    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")


async def run_case(episode_id: str):
    episodes = load_episodes(DATA_PATH)
    case = next(c for c in extract_cascade_cases(episodes) if c.episode_id == episode_id)
    print(f"case {episode_id}: target={case.target_entity} gold={case.gold_answer} hop={case.hop}")
    print(f"  cascade_source={case.cascade_source} root={case.root} root_change={case.root_change}")

    system = await build_system(chat_model="gpt-4o-mini")
    try:
        await _truncate(system)
        embed = system.embedding.embed
        embed_batch = system.embedding.embed_batch

        async with system.repo.session(ACCOUNT) as db:
            graph = await ingest_case(db, case, ACCOUNT, embed_batch)
            n_filler = await ingest_filler(db, case, ACCOUNT, embed_batch, granularity="session")
        print(f"  ingested: root + {len(graph.materialized)} materialized + "
              f"{len(graph.predeclarations)} predecl + {n_filler} filler nodes")

        # before-question (pre-change state)
        before_res = None
        if case.before_question:
            async with system.repo.session(ACCOUNT) as db:
                before_res = await answer_question(system, db, ACCOUNT, case.before_question.question)
            print(f"  BEFORE: {before_res.answer!r}")

        async with system.repo.session(ACCOUNT) as db:
            await apply_root_change(db, case, graph, embed)

        # OFF
        async with system.repo.session(ACCOUNT) as db:
            off_res = await answer_question(system, db, ACCOUNT, case.after_question.question)
        print(f"  OFF: {off_res.answer!r}")

        # ON: drain
        engine = system.build_engine(cascade_on_stale=True)
        engine._running = True
        for _ in range(8):
            await engine._drain_ready_events(context_id=None)

        # per-materialized-node status (proves each hop staled)
        async with system.repo.session(ACCOUNT) as db:
            rows = await db.fetch(
                "SELECT uri, status FROM contexts WHERE uri NOT LIKE '%filler%' ORDER BY uri"
            )
            for r in rows:
                print(f"    {r['status']:8s} {r['uri'].split('/memories/')[-1]}")
            on_res = await answer_question(system, db, ACCOUNT, case.after_question.question)
        print(f"  ON: {on_res.answer!r}")

        before_gold = case.before_question.expected_answer if case.before_question else None
        off_v = judge_case(before_res.answer if before_res else "", before_gold,
                           off_res.answer, case.after_question.expected_answer)
        on_v = judge_case(before_res.answer if before_res else "", before_gold,
                          on_res.answer, case.after_question.expected_answer)
        print("\n=== VERDICT ===")
        print(f"  before_ok={on_v.before_ok}")
        print(f"  OFF after_ok={off_v.after_ok} trivial_pass={off_v.trivial_pass}")
        print(f"  ON  after_ok={on_v.after_ok} trivial_pass={on_v.trivial_pass}")
        print(f"  oracle_calls={system.oracle_chat.call_count} answer_calls={system.answer_chat.call_count}")
        ok = (not off_v.after_ok) and on_v.after_ok
        print(f"\n  GO/NO-GO: {'PASS' if ok else 'FAIL'} (expect OFF wrong, ON right)")
    finally:
        await system.close()


if __name__ == "__main__":
    episode = sys.argv[1] if len(sys.argv) > 1 else "sw_033"
    asyncio.run(run_case(episode))
