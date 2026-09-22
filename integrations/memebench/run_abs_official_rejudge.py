"""Re-score the frozen `Abs` runs with MEME's own judge prompts (Figures 18/24).

Offline rejudge: reads answers already on disk, re-grades them under the
paper's published criterion, and writes a side-by-side report. Generation is
never re-run and the frozen artifacts are never written -- their content hashes
are taken before and after and compared, so "we did not touch the runs" is
checked rather than asserted.

    # no API calls, verifies every input and prints the plan
    .venv/bin/python -m integrations.memebench.run_abs_official_rejudge preflight

    # a handful of real calls, same code path as the formal run
    ... run_abs_official_rejudge smoke --limit 3 \
        --judge-model gpt-4o --provider openlux

    # all 357 gradings, resumable
    ... run_abs_official_rejudge run --judge-model gpt-4o --provider openlux

Model and provider have no defaults: a silent default once polluted a whole
batch, so a missing flag refuses to run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from contexthub.llm.chat_client import OpenAIChatClient
from integrations.memebench.abs_official_rejudge import (
    JudgeCall,
    RejudgeError,
    build_case_rows,
    build_plan,
    build_report,
    load_artifacts,
    notices_requested,
    read_checkpoint,
    run_identity,
    sha256_text,
    usage_attribution_audit,
    usd_cost,
    verdicts_from_rows,
)
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.meme_official_judge import (
    ABS_JUDGE_PROMPT,
    BEFORE_JUDGE_PROMPT,
    JUDGE_MAX_TOKENS,
    JUDGE_TEMPERATURE,
    KNOWN_TRANSCRIPTION_LIMITATION,
    appears_verbatim_in_paper,
    paper_text_path,
    parse_verdict,
)
from integrations.memebench.systems import load_provider

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
DEFAULT_OUT = RUNS / "abs_official_rejudge_20260902"
INPUT_RUNS = {
    "hop1": RUNS / "formal_abs_hop1_20260902_final",
    "hop2": RUNS / "formal_abs_hop2_20260902_final",
}
EXPECTED_EPISODES = {"hop1": 90, "hop2": 29}
EXPECTED_CALLS = 357
DEFAULT_PRICE_TABLE = HERE / "full100_v2_price_table.json"


def parse_run_spec(values: Sequence[str] | None) -> dict[str, Path] | None:
    """``name=path`` pairs into a run map, or None to keep the frozen default.

    Paths are resolved so the stamped identity records where the artifacts
    actually came from rather than how the caller happened to spell it.
    """
    if not values:
        return None
    runs: dict[str, Path] = {}
    for item in values:
        name, _, raw = item.partition("=")
        if not name or not raw:
            raise RejudgeError(f"--runs expects name=path, got {item!r}")
        if name in runs:
            raise RejudgeError(f"--runs names must be unique, {name!r} repeated")
        runs[name] = Path(raw).expanduser().resolve()
    return runs


def parse_episode_spec(values: Sequence[str] | None) -> dict[str, int] | None:
    """``name=count`` pairs, the per-run denominators preflight will enforce."""
    if not values:
        return None
    out: dict[str, int] = {}
    for item in values:
        name, _, raw = item.partition("=")
        if not name or not raw:
            raise RejudgeError(f"--expect-episodes expects name=count, got {item!r}")
        try:
            out[name] = int(raw)
        except ValueError as exc:
            raise RejudgeError(f"--expect-episodes count must be an int: {item!r}") from exc
    return out


def resolve_inputs(args: argparse.Namespace) -> tuple[dict[str, Path], dict[str, int], int]:
    """The (runs, per-run episode counts, total calls) this invocation will enforce.

    With no ``--runs`` this returns the frozen defaults byte-for-byte, so the
    published command keeps stamping the same identity. With ``--runs`` the
    expected counts must be given explicitly: dropping the count check for a new
    run would remove the only guard that a directory is complete, and a rejudge
    over a partial run silently changes every denominator it reports.
    """
    runs = parse_run_spec(args.runs)
    if runs is None:
        return dict(INPUT_RUNS), dict(EXPECTED_EPISODES), EXPECTED_CALLS

    episodes = parse_episode_spec(args.expect_episodes)
    if not episodes:
        raise RejudgeError("--runs requires --expect-episodes name=count for every run")
    missing = sorted(set(runs) - set(episodes))
    extra = sorted(set(episodes) - set(runs))
    if missing or extra:
        raise RejudgeError(
            f"--expect-episodes must name exactly the --runs entries "
            f"(missing={missing}, unknown={extra})"
        )
    # Three gradings per episode (before/off/on); trivial-pass needs all three,
    # so a plan that is not exactly 3x the episodes means a partial case.
    expected_calls = args.expected_calls
    if expected_calls is None:
        expected_calls = 3 * sum(episodes.values())
    return runs, episodes, expected_calls


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def artifact_hashes(runs: Mapping[str, Path]) -> dict[str, str]:
    """sha256 of every frozen artifact, so mutation is detectable, not assumed."""
    out = {}
    for run, run_dir in sorted(runs.items()):
        for path in sorted((run_dir / "artifacts" / "full-run").glob("*/artifact.json")):
            out[f"{run}/{path.parent.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --- preflight -----------------------------------------------------------------


def preflight(
    runs: Mapping[str, Path],
    price_table: Path,
    judge_model: str | None,
    *,
    expected_episodes: Mapping[str, int] | None = None,
    expected_calls: int | None = None,
) -> dict[str, Any]:
    """Everything that must hold before a paid call. Raises on any failure."""
    expected_episodes = EXPECTED_EPISODES if expected_episodes is None else expected_episodes
    expected_calls = EXPECTED_CALLS if expected_calls is None else expected_calls
    checks: dict[str, Any] = {}

    paper = paper_text_path()
    if not paper.exists():
        raise RejudgeError(f"paper text missing: {paper}")
    text = paper.read_text(encoding="utf-8")
    for name, template in (("absence", ABS_JUDGE_PROMPT), ("before", BEFORE_JUDGE_PROMPT)):
        if not appears_verbatim_in_paper(template, text):
            raise RejudgeError(f"{name} judge prompt is not verbatim in the paper")
    checks["prompts_verbatim_in_paper"] = True
    checks["paper_sha256"] = hashlib.sha256(paper.read_bytes()).hexdigest()

    notices: dict[str, bool] = {}
    for run, run_dir in runs.items():
        if not run_dir.exists():
            raise RejudgeError(f"input run missing: {run_dir}")
        if run not in expected_episodes:
            raise RejudgeError(f"no expected episode count for run {run!r}")
        artifacts = load_artifacts(run_dir)
        if len(artifacts) != expected_episodes[run]:
            raise RejudgeError(
                f"{run}: expected {expected_episodes[run]} artifacts, found {len(artifacts)}"
            )
        # Recorded, not inferred: it decides which strata the report may emit, and
        # it is the only machine-readable proof that a no-notices arm really ran
        # with notices off.
        notices[run] = notices_requested(artifacts)
    plan = build_plan(runs)          # raises on any inconsistent case
    if len(plan) != expected_calls:
        raise RejudgeError(f"expected {expected_calls} calls, planned {len(plan)}")
    checks["n_calls"] = len(plan)
    checks["episodes"] = {run: expected_episodes[run] for run in runs}
    checks["with_stale_notices"] = notices

    prices = json.loads(price_table.read_text(encoding="utf-8"))
    if judge_model is not None and judge_model not in prices:
        raise RejudgeError(f"no price for judge model {judge_model!r} in {price_table}")
    checks["price_table_sha256"] = hashlib.sha256(price_table.read_bytes()).hexdigest()
    checks["known_transcription_limitation"] = KNOWN_TRANSCRIPTION_LIMITATION
    checks["source_bundle_note"] = (
        "This rejudge added an optional `temperature` kwarg to "
        "src/contexthub/llm/chat_client.py so the paper's GPT-4o temperature 0 "
        "could actually be sent (it was previously never sent). That file is "
        "hashed into the frozen runs' authorization.input_bundle, so their "
        "verify_run_identity now flags it. Nothing in those runs was re-executed "
        "and their artifacts are byte-identical (checked each run). Note the same "
        "bundles were already unverifiable via model_providers.local.json, a "
        "secrets file expected to drift. Existing callers are unaffected: the "
        "kwarg defaults to None and is omitted from the payload when unset."
    )
    return {"checks": checks, "plan": plan, "prices": prices}


# --- paid loop -----------------------------------------------------------------


async def grade(
    chat: CountingChatClient,
    call: JudgeCall,
    price: Mapping[str, Any],
) -> dict[str, Any]:
    """One paid grading, recording its own usage delta and cost."""
    before = (chat.call_count, chat.prompt_tokens, chat.completion_tokens, chat.retry_attempts)
    started = time.monotonic()
    raw = await chat.complete(call.prompt, max_tokens=JUDGE_MAX_TOKENS)
    elapsed = time.monotonic() - started
    prompt_tokens = chat.prompt_tokens - before[1]
    completion_tokens = chat.completion_tokens - before[2]
    verdict = parse_verdict(raw)
    return {
        "key": call.key,
        "run": call.run,
        "episode_id": call.episode_id,
        "hop": call.hop,
        "stage": call.stage,
        "question": call.question,
        "gold": call.gold,
        "answer": call.answer,
        "prompt_sha256": sha256_text(call.prompt),
        "raw_output": raw,
        "correct": verdict.correct,
        "reason": verdict.reason,
        "parse_failed": verdict.parse_failed,
        "usage": {
            "calls": chat.call_count - before[0],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "retry_attempts": chat.retry_attempts - before[3],
            "tokens_are_real": chat.tokens_are_real,
        },
        "cost_usd": usd_cost(prompt_tokens, completion_tokens, price),
        "elapsed_s": round(elapsed, 3),
    }


async def run_plan(
    plan: Sequence[JudgeCall],
    *,
    judge_model: str,
    provider_label: str,
    providers_path: Path,
    price: Mapping[str, Any],
    checkpoint_path: Path,
    concurrency: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Grade every outstanding call, appending to the checkpoint as they land.

    Returns (all rows incl. resumed, this process's client snapshots, keys graded
    now). The last two feed :func:`usage_attribution_audit`.
    """
    done = read_checkpoint(checkpoint_path)
    todo = [c for c in plan if c.key not in done]
    print(f"  {len(done)} already graded, {len(todo)} to go")
    if not todo:
        return done, [], set()

    provider = load_provider(provider_label, providers_path)
    # ONE CLIENT PER CONCURRENCY SLOT. CountingChatClient counts into shared
    # mutable fields, so grade()'s before/after delta is only correct while
    # nothing else is using that client. Sharing one client across concurrent
    # calls made every per-call number absorb its neighbours' tokens (measured
    # 2026-09-02: a 221-token prompt recorded as 2201, a 4x aggregate inflation).
    # Every other memebench script calls sequentially, which is why this never
    # bit before. A worker owns its client for the whole run, so each client
    # serves exactly one call at a time and the deltas are exact.
    n_workers = max(1, min(concurrency, len(todo)))
    clients = [
        CountingChatClient(
            OpenAIChatClient(
                api_key=provider["api_key"],
                base_url=provider["base_url"],
                model=judge_model,
                temperature=JUDGE_TEMPERATURE,   # the paper pins temperature 0
            ),
            model=judge_model,
        )
        for _ in range(n_workers)
    ]
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[JudgeCall] = asyncio.Queue()
    for call in todo:
        queue.put_nowait(call)
    lock = asyncio.Lock()
    completed = 0

    async def worker(chat: CountingChatClient) -> None:
        nonlocal completed
        while True:
            try:
                call = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            row = await grade(chat, call, price)
            async with lock:
                with checkpoint_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                done[row["key"]] = row
                completed += 1
                if completed % 25 == 0 or completed == len(todo):
                    spent = sum(float(r.get("cost_usd") or 0) for r in done.values())
                    print(f"  {completed}/{len(todo)} graded, ${spent:.4f} so far")

    try:
        await asyncio.gather(*(worker(chat) for chat in clients))
    finally:
        for chat in clients:
            await chat.close()
    return done, [chat.snapshot() for chat in clients], {c.key for c in todo}


# --- reporting -----------------------------------------------------------------


def assemble(
    runs: Mapping[str, Path],
    rows: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    verdicts = verdicts_from_rows(rows)
    per_run, cases = {}, {}
    for run, run_dir in runs.items():
        artifacts = load_artifacts(run_dir)
        case_rows = build_case_rows(artifacts, verdicts, run=run)
        per_run[run] = (artifacts, case_rows)
        cases[run] = case_rows
    return build_report(per_run), cases


def cost_summary(rows: Mapping[str, Mapping[str, Any]], judge_model: str) -> dict[str, Any]:
    usages = [r.get("usage") or {} for r in rows.values()]
    return {
        "judge_model": judge_model,
        "n_calls": len(rows),
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
        "retry_attempts": sum(int(u.get("retry_attempts") or 0) for u in usages),
        "tokens_are_real": all(bool(u.get("tokens_are_real")) for u in usages) if usages else False,
        "total_usd": sum(float(r.get("cost_usd") or 0) for r in rows.values()),
        "parse_failures": sum(1 for r in rows.values() if r.get("parse_failed")),
    }


def print_report(report: Mapping[str, Any]) -> None:
    def pct(part: int | None, whole: int) -> str:
        if not whole or part is None:
            return "     n/a"
        return f"{part:3d}/{whole:<3d} ({part / whole * 100:5.1f}%)"

    for run, block in report["runs"].items():
        print(f"\n{'=' * 78}\n### {run}  hop={block['hop']}  n={block['n_episodes']}\n{'=' * 78}")
        for name, s in block["strata"].items():
            if not s["n"]:
                continue
            print(f"\n  [{name}]  n={s['n']}   before(MEME): {pct(s.get('before_official_ok'), s['n'])}")
            print(f"    {'arm':5s} {'MEME raw':>17s} {'MEME +trivial':>17s} {'ours (3-part)':>17s} {'disagree':>9s}")
            for arm in ("off", "on"):
                row = s.get(arm)
                if not row:
                    continue
                print(
                    f"    {arm:5s} "
                    f"{pct(row['official_raw'], s['n']):>17s} "
                    f"{pct(row['official_trivial_pass'], s['n']):>17s} "
                    f"{pct(row['three_part'], s['n']):>17s} "
                    f"{row['n_disagreements']:>9d}"
                )
            if s.get("parse_failures"):
                print(f"    parse failures: {s['parse_failures']}")


# --- CLI -----------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    runs, expected_episodes, expected_calls = resolve_inputs(args)
    if args.runs and args.out == DEFAULT_OUT:
        # The default directory holds the published 357-call rejudge. A run over
        # different inputs writes a different report and identity, so landing it
        # there would overwrite the frozen result with something incomparable.
        raise RejudgeError(
            "--runs writes a different report; pass --out to a new directory "
            f"instead of the published {DEFAULT_OUT.name}"
        )
    pre = preflight(
        runs,
        args.price_table,
        args.judge_model,
        expected_episodes=expected_episodes,
        expected_calls=expected_calls,
    )
    plan: list[JudgeCall] = pre["plan"]
    print(f"preflight OK: {len(plan)} calls, prompts verbatim in the paper")
    for run, requested in sorted(pre["checks"]["with_stale_notices"].items()):
        print(f"  {run}: with_stale_notices={requested}")

    if args.command == "preflight":
        write_json(args.out / "preflight.json", pre["checks"])
        print(f"wrote {args.out / 'preflight.json'}")
        return 0

    if args.limit:
        # Whole episodes only: a partial case cannot be trivial-pass scored.
        keep = [c.key for c in plan][: args.limit * 3]
        plan = [c for c in plan if c.key in set(keep)]
        print(f"  limited to {len(plan)} calls ({args.limit} episodes)")

    price = pre["prices"][args.judge_model]
    hashes_before = artifact_hashes(runs)
    identity = run_identity(
        judge_model=args.judge_model, provider=args.provider, runs=runs, plan=plan
    )
    identity["command"] = args.command
    identity["preflight"] = pre["checks"]
    write_json(args.out / "identity.json", identity)

    started = time.monotonic()
    rows, client_totals, graded_now = await run_plan(
        plan,
        judge_model=args.judge_model,
        provider_label=args.provider,
        providers_path=args.providers_path,
        price=price,
        checkpoint_path=args.out / "checkpoint.jsonl",
        concurrency=args.concurrency,
    )
    elapsed = time.monotonic() - started

    if artifact_hashes(runs) != hashes_before:
        raise RejudgeError("frozen artifacts changed during the rejudge")
    print("  verified: frozen artifacts byte-identical")

    graded = {k: v for k, v in rows.items() if k in {c.key for c in plan}}

    # Per-call usage must reconcile with the clients' own totals, or the cost
    # table is fiction. Only calls made in THIS process are auditable; resumed
    # rows were counted by a process whose clients no longer exist.
    audit = usage_attribution_audit(
        [rows[k] for k in sorted(graded_now) if k in rows], client_totals
    )
    if not audit["ok"]:
        raise RejudgeError(f"usage attribution mismatch, cost is unreliable: {audit}")
    print(
        f"  verified: usage reconciles across {audit['n_clients']} clients "
        f"({audit['n_rows_audited']} calls this process)"
    )

    costs = cost_summary(graded, args.judge_model)
    costs["wall_clock_s"] = round(elapsed, 1)
    costs["usage_attribution_audit"] = audit
    costs["resumed_calls_not_audited"] = len(graded) - len(graded_now & set(graded))
    write_json(args.out / "cost.json", costs)
    print(
        f"\n{costs['n_calls']} gradings, ${costs['total_usd']:.4f}, "
        f"{costs['parse_failures']} parse failures, {elapsed:.0f}s"
    )

    if args.command == "smoke":
        write_json(args.out / "smoke_rows.json", sorted(graded.values(), key=lambda r: r["key"]))
        for row in sorted(graded.values(), key=lambda r: r["key"]):
            print(f"  {row['key']:24s} correct={row['correct']!s:5s} {row['reason'][:70]}")
        return 0

    report, cases = assemble(runs, graded)
    report["cost"] = costs
    report["identity"] = identity
    write_json(args.out / "report.json", report)
    write_json(args.out / "cases.json", cases)
    print_report(report)
    print(f"\nwrote {args.out / 'report.json'} and cases.json")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "smoke", "run"])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, help="episodes (not calls) for a smoke")
    # No defaults: a silent model default once polluted a whole batch.
    parser.add_argument("--judge-model", help="required for smoke/run")
    parser.add_argument("--provider", help="required for smoke/run")
    parser.add_argument("--providers-path", type=Path, default=None)
    parser.add_argument("--price-table", type=Path, default=DEFAULT_PRICE_TABLE)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--runs",
        action="append",
        metavar="NAME=PATH",
        help=(
            "Rejudge a different frozen run instead of the published pair, e.g. "
            "--runs hop1_nonotices=integrations/memebench/runs/abs_hop1_nonotices_20260907. "
            "Repeatable. Requires --expect-episodes and a non-default --out."
        ),
    )
    parser.add_argument(
        "--expect-episodes",
        action="append",
        metavar="NAME=COUNT",
        help=(
            "Per-run episode count preflight must find, one per --runs entry. "
            "Required with --runs: without it nothing checks the directory is complete."
        ),
    )
    parser.add_argument(
        "--expected-calls",
        type=int,
        help=(
            "Override the total grading count preflight enforces. Defaults to "
            "3x the summed --expect-episodes (before/off/on per episode)."
        ),
    )
    args = parser.parse_args()

    if args.command in ("smoke", "run"):
        missing = [n for n in ("judge_model", "provider") if not getattr(args, n)]
        if missing:
            parser.error(
                "--judge-model and --provider have no defaults; missing: "
                + ", ".join(f"--{n.replace('_', '-')}" for n in missing)
            )
    if args.providers_path is None:
        from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH
        args.providers_path = DEFAULT_PROVIDERS_PATH
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
