"""M2 go/no-go smoke: single 1-hop case (sw_033) end-to-end, paired OFF/ON.

Run: CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench._m2_smoke
Requires PG on localhost:5432 and yunwu keys in model_providers.local.json.
"""

from __future__ import annotations

import asyncio

from integrations.memebench.answer import answer_question
from integrations.memebench.ingest import apply_root_change, ingest_case, ingest_filler
from integrations.memebench.judge import judge_case
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.systems import build_system

ACCOUNT = "meme-eval"
DATA_PATH = "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"


async def _truncate(system):
    async with system.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE contexts, dependencies, change_events, audit_log CASCADE"
        )


async def main():
    episodes = load_episodes(DATA_PATH)
    case = next(c for c in extract_cascade_cases(episodes) if c.episode_id == "sw_033")
    print(f"case sw_033: target={case.target_entity} gold={case.gold_answer} hop={case.hop}")
    print(f"  before_q: {case.before_question.question!r} -> {case.before_question.expected_answer}")
    print(f"  after_q : {case.after_question.question!r} -> {case.after_question.expected_answer}")

    system = await build_system(chat_model="gpt-4o-mini")
    try:
        await _truncate(system)

        embed = system.embedding.embed

        # Stage B: ingest pre-change graph + filler noise.
        async with system.repo.session(ACCOUNT) as db:
            graph = await ingest_case(db, case, ACCOUNT, embed)
            n_filler = await ingest_filler(db, case, ACCOUNT, embed, granularity="session")
        print(f"  ingested: root + {len(graph.materialized)} materialized + "
              f"{len(graph.predeclarations)} predeclaration + {n_filler} filler-noise nodes")

        # before-question: ask while graph is still in before state.
        async with system.repo.session(ACCOUNT) as db:
            before_res = await answer_question(system, db, ACCOUNT, case.before_question.question)
        print(f"  BEFORE answer: {before_res.answer!r} (retrieved {len(before_res.retrieved_uris)} notes)")

        # Stage C: apply root_change.
        async with system.repo.session(ACCOUNT) as db:
            await apply_root_change(db, case, graph, embed)

        # OFF arm: no propagation. Poisoned node still active.
        async with system.repo.session(ACCOUNT) as db:
            off_res = await answer_question(system, db, ACCOUNT, case.after_question.question)
        print(f"  OFF  answer: {off_res.answer!r}")

        # ON arm: drain propagation (oracle marks poisoned node stale).
        engine = system.build_engine(cascade_on_stale=True)
        engine._running = True
        for _ in range(5):
            await engine._drain_ready_events(context_id=None)

        # inspect statuses
        async with system.repo.session(ACCOUNT) as db:
            rows = await db.fetch(
                "SELECT uri, status FROM contexts WHERE uri NOT LIKE '%filler%' ORDER BY uri"
            )
            for r in rows:
                print(f"    status: {r['status']:8s} {r['uri']}")
            on_res = await answer_question(system, db, ACCOUNT, case.after_question.question)
        print(f"  ON   answer: {on_res.answer!r}")

        # Judge
        off_v = judge_case(before_res.answer, case.before_question.expected_answer,
                           off_res.answer, case.after_question.expected_answer)
        on_v = judge_case(before_res.answer, case.before_question.expected_answer,
                          on_res.answer, case.after_question.expected_answer)
        print("\n=== VERDICT ===")
        print(f"  before_ok={off_v.before_ok}")
        print(f"  OFF: after_ok={off_v.after_ok}  trivial_pass={off_v.trivial_pass}")
        print(f"  ON : after_ok={on_v.after_ok}  trivial_pass={on_v.trivial_pass}")
        print(f"  oracle calls={system.oracle_chat.call_count}  answer calls={system.answer_chat.call_count}")

        # go/no-go expectations
        ok = (not off_v.after_ok) and on_v.after_ok
        print(f"\n  GO/NO-GO: {'PASS' if ok else 'FAIL'} "
              f"(expect OFF wrong, ON right)")
    finally:
        await system.close()


if __name__ == "__main__":
    asyncio.run(main())
