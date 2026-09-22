"""Offline re-scoring of the frozen `Abs` runs under MEME's published criterion.

This is a *second reading of answers already on disk*: no generation is re-run,
no artifact is mutated. The frozen run directories carry authorization input
hashes, so results land in a separate rejudge directory and the artifacts stay
byte-identical; the two criteria are then reported side by side, with per-case
disagreements listed rather than one silently winning.

Everything here is pure: building the call plan, parsing, aggregation and
stratification. The paid loop lives in ``run_abs_official_rejudge.py``, so the
arithmetic that decides the reported numbers is testable without an API key.

Stratification, and why each stratum exists:

* **hop1 no-signal (derived)** -- episodes whose ON arm received *zero* stale
  notices. The ON arm is defined by those notices, so with none it degenerates
  into a copy of OFF and cannot exhibit propagation either way. Derived from the
  artifacts (``stale_notices == []``) rather than hardcoded, so a rerun that
  fixes them reclassifies them automatically. Note this is sharper than
  ``p1_path_status == "p1-system-miss"``: on hop1, 13 episodes miss the path but
  only 3 end up with no notice at all.
* **hop2 missing-edge** -- the 8 episodes whose dependency chain is real in the
  dataset's fact fields but absent from the frozen v3 graph. Reused from
  :mod:`abs_report` so both reports partition hop2 identically. They stay in the
  denominator on purpose: a P1 recall hole, not a dataset defect.
* **domain** -- `pl` vs `sw`, which the plan expects to differ sharply.
"""

from __future__ import annotations

import glob
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from integrations.memebench.abs_report import HOP2_MISSING_EDGE
from integrations.memebench.meme_official_judge import (
    ABS_JUDGE_PROMPT,
    BEFORE_JUDGE_PROMPT,
    JUDGE_MAX_TOKENS,
    JUDGE_TEMPERATURE,
    STAGES,
    OfficialVerdict,
    parse_verdict,
    prompt_for_stage,
    render,
    score_case,
)


class RejudgeError(RuntimeError):
    """Raised when the inputs are not in a state that can be judged."""


# --- inputs --------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeCall:
    """One (episode, stage) grading, keyed so a resumed run can skip it."""

    run: str
    episode_id: str
    hop: int
    stage: str
    question: str
    gold: str
    answer: str
    prompt: str

    @property
    def key(self) -> str:
        return f"{self.run}|{self.episode_id}|{self.stage}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    """Every full-run artifact in a frozen run directory, ordered by episode."""
    paths = sorted(
        glob.glob(str(run_dir / "artifacts" / "full-run" / "*" / "artifact.json"))
    )
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]


def _judge_rows(artifact: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("stage")): row
        for row in ((artifact.get("judge") or {}).get("calls") or [])
    }


def build_calls(artifact: Mapping[str, Any], *, run: str) -> list[JudgeCall]:
    """The three gradings for one episode, reusing the recorded questions/golds.

    Questions and golds are taken from the artifact's own judge rows so this
    rejudge grades exactly what the original judge graded; only the *prompt*
    changes. Missing any of the three stages is an error rather than a silent
    partial case, since trivial-pass needs the before stage.
    """
    rows = _judge_rows(artifact)
    episode_id = str(artifact.get("episode_id"))
    missing = [stage for stage in STAGES if stage not in rows]
    if missing:
        raise RejudgeError(f"{episode_id}: judge rows missing for {missing}")

    hop = int(artifact.get("evaluation_hop") or 1)
    calls = []
    for stage in STAGES:
        row = rows[stage]
        # The answer is re-read from answers.<stage>.raw_answer and cross-checked
        # against the judge row, so a rejudge can never grade a different string
        # than the run recorded.
        recorded = ((artifact.get("answers") or {}).get(stage) or {}).get("raw_answer")
        if row.get("answer") != recorded:
            raise RejudgeError(
                f"{episode_id}/{stage}: judge row answer differs from raw_answer"
            )
        gold = row.get("gold")
        if not (gold or "").strip():
            raise RejudgeError(f"{episode_id}/{stage}: empty gold")
        question = str(row.get("question") or "")
        # An empty answer is a real measurement (the model returned nothing), so
        # it is graded like any other rather than skipped.
        answer = str(recorded or "")
        template = prompt_for_stage(stage)
        calls.append(
            JudgeCall(
                run=run,
                episode_id=episode_id,
                hop=hop,
                stage=stage,
                question=question,
                gold=str(gold),
                answer=answer,
                prompt=render(
                    template, question=question, gold=str(gold), agent_answer=answer
                ),
            )
        )
    return calls


def build_plan(runs: Mapping[str, Path]) -> list[JudgeCall]:
    """Every grading across every run directory, in a stable order."""
    plan: list[JudgeCall] = []
    for run, run_dir in runs.items():
        artifacts = load_artifacts(run_dir)
        if not artifacts:
            raise RejudgeError(f"{run}: no full-run artifacts under {run_dir}")
        for artifact in artifacts:
            if artifact.get("task_type") != "Abs":
                raise RejudgeError(
                    f"{run}/{artifact.get('episode_id')}: task_type is not Abs"
                )
            plan.extend(build_calls(artifact, run=run))
    return plan


# --- existing (three-part) criterion, read back for comparison -----------------


def existing_verdicts(artifact: Mapping[str, Any]) -> dict[str, bool]:
    """Our own three-part criterion per stage (containment for `before`)."""
    stages = ((artifact.get("judge") or {}).get("abs_scoring") or {}).get("stages") or {}
    return {
        stage: bool((stages.get(stage) or {}).get("correct"))
        for stage in STAGES
        if stage in stages
    }


# --- stratification ------------------------------------------------------------


def no_signal_episodes(artifacts: Iterable[Mapping[str, Any]]) -> set[str]:
    """Episodes whose ON arm got no stale notice, so ON could not differ from OFF."""
    out = set()
    for artifact in artifacts:
        notices = (
            ((artifact.get("answers") or {}).get("on") or {})
            .get("retrieval", {})
            .get("stale_notices")
        ) or []
        if not notices:
            out.add(str(artifact.get("episode_id")))
    return out


def notices_requested(artifacts: Sequence[Mapping[str, Any]]) -> bool:
    """Whether this run asked retrieval for stale notices at all.

    Read from the artifact's own ``with_stale_notices`` rather than inferred, and
    never defaulted: a run executed with ``--no-stale-notices`` has an empty
    ``stale_notices`` on every episode *by construction*, which is indistinguishable
    from "propagation produced no signal" if you only look at the notices. Guessing
    this field is the same defect class that silently mis-scored whole batches via a
    defaulted ``task_type``, so a missing value raises instead.

    A run must be uniform: mixing the two within one directory would make every
    notice-derived stratum mean two different things at once.
    """
    values = set()
    for artifact in artifacts:
        if "with_stale_notices" not in artifact:
            raise RejudgeError(
                f"{artifact.get('episode_id')}: artifact has no with_stale_notices; "
                "refusing to guess whether notices were requested"
            )
        values.add(bool(artifact["with_stale_notices"]))
    if len(values) > 1:
        raise RejudgeError("run mixes with_stale_notices=True and False artifacts")
    return values.pop() if values else True


def strata(artifacts: Sequence[Mapping[str, Any]], *, hop: int) -> dict[str, set[str]]:
    """Named episode subsets for one hop. Every stratum is a subset of ALL.

    On hop1 the notice-derived split is emitted only when the run actually asked
    for notices. Without that guard a ``--no-stale-notices`` run reports
    ``no_signal_on`` = every episode and ``signal_on`` = empty, which reads as
    "propagation produced no signal anywhere" when propagation in fact ran
    normally and was merely not explained to the model. Omitting the stratum
    makes the absence visible; emitting a degenerate one hides it.
    """
    ids = {str(a.get("episode_id")) for a in artifacts}
    out: dict[str, set[str]] = {"all": set(ids)}
    for domain in ("pl", "sw"):
        out[f"domain_{domain}"] = {e for e in ids if e.startswith(domain)}
    if hop == 1 and notices_requested(artifacts):
        no_signal = no_signal_episodes(artifacts) & ids
        out["no_signal_on"] = no_signal
        out["signal_on"] = ids - no_signal
    if hop == 2:
        missing = ids & set(HOP2_MISSING_EDGE)
        out["missing_edge"] = missing
        out["root_reachable"] = ids - missing
    return out


# --- aggregation ---------------------------------------------------------------


def _rate(part: int, whole: int) -> float | None:
    return (part / whole) if whole else None


def summarize(
    rows: Sequence[Mapping[str, Any]], episode_ids: Iterable[str] | None = None
) -> dict[str, Any]:
    """Counts for one stratum under both criteria, plus their disagreements.

    ``rows`` are per-episode records from :func:`build_case_rows`. Reported for
    each arm: the official raw pass rate, the official rate gated by MEME's
    trivial-pass rule, and our three-part rate over the same episodes.
    """
    if episode_ids is not None:
        wanted = set(episode_ids)
        rows = [r for r in rows if r["episode_id"] in wanted]
    n = len(rows)
    out: dict[str, Any] = {"n": n, "episode_ids": sorted(r["episode_id"] for r in rows)}
    if not n:
        return out

    out["before_official_ok"] = sum(1 for r in rows if r["official"]["before_ok"])
    out["before_three_part_ok"] = sum(1 for r in rows if r["existing"].get("before"))
    out["parse_failures"] = sum(len(r["official"]["parse_failures"]) for r in rows)

    for arm in ("off", "on"):
        raw = sum(1 for r in rows if r["official"].get(f"raw_{arm}_ok"))
        gated = sum(1 for r in rows if r["official"].get(f"trivial_pass_{arm}"))
        ours = sum(1 for r in rows if r["existing"].get(arm))
        disagree = [
            r["episode_id"]
            for r in rows
            if bool(r["official"].get(f"raw_{arm}_ok")) != bool(r["existing"].get(arm))
        ]
        out[arm] = {
            "official_raw": raw,
            "official_raw_rate": _rate(raw, n),
            "official_trivial_pass": gated,
            "official_trivial_pass_rate": _rate(gated, n),
            "three_part": ours,
            "three_part_rate": _rate(ours, n),
            "disagreements": sorted(disagree),
            "n_disagreements": len(disagree),
        }
    return out


def build_case_rows(
    artifacts: Sequence[Mapping[str, Any]],
    verdicts: Mapping[str, OfficialVerdict],
    *,
    run: str,
) -> list[dict[str, Any]]:
    """Join each artifact with its three official verdicts.

    ``verdicts`` is keyed by :attr:`JudgeCall.key`. A missing verdict is an error:
    scoring a partial case would quietly change the denominator.
    """
    rows = []
    for artifact in artifacts:
        episode_id = str(artifact.get("episode_id"))
        case: dict[str, OfficialVerdict] = {}
        for stage in STAGES:
            key = f"{run}|{episode_id}|{stage}"
            if key not in verdicts:
                raise RejudgeError(f"missing official verdict for {key}")
            case[stage] = verdicts[key]
        rows.append(
            {
                "episode_id": episode_id,
                "hop": int(artifact.get("evaluation_hop") or 1),
                "official": score_case(case),
                "existing": existing_verdicts(artifact),
                "p1_path_status": artifact.get("p1_path_status"),
                "replacement_visibility_on": artifact.get("replacement_visibility_on"),
                "on_stale_notice_count": len(
                    ((artifact.get("answers") or {}).get("on") or {})
                    .get("retrieval", {})
                    .get("stale_notices")
                    or []
                ),
                "answers": {
                    stage: ((artifact.get("answers") or {}).get(stage) or {}).get(
                        "raw_answer"
                    )
                    for stage in STAGES
                },
                "gold": (artifact.get("retrieval_evidence_contract") or {}).get(
                    "replacement_reference"
                ),
            }
        )
    return rows


def build_report(
    per_run: Mapping[str, tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    """Stratified report across runs. Values are ``(artifacts, case_rows)``."""
    report: dict[str, Any] = {"criterion": "meme_official_figure24", "runs": {}}
    for run, (artifacts, rows) in per_run.items():
        hop = int(artifacts[0].get("evaluation_hop") or 1) if artifacts else 1
        requested = notices_requested(artifacts) if artifacts else True
        block: dict[str, Any] = {
            "hop": hop,
            "n_episodes": len(rows),
            "with_stale_notices": requested,
            "strata": {
                name: summarize(rows, ids)
                for name, ids in strata(artifacts, hop=hop).items()
            },
        }
        if hop == 1 and not requested:
            # Say why the stratum is absent, so a reader comparing two reports
            # does not read the gap as a formatting difference.
            block["omitted_strata"] = {
                "no_signal_on": "run used --no-stale-notices: every episode has "
                "zero notices by construction, so the notice-derived split "
                "carries no information about propagation",
                "signal_on": "same",
            }
        report["runs"][run] = block
    return report


# --- run identity --------------------------------------------------------------


def run_identity(
    *,
    judge_model: str,
    provider: str,
    runs: Mapping[str, Path],
    plan: Sequence[JudgeCall],
) -> dict[str, Any]:
    """What must match for two rejudge runs to be comparable.

    The two prompt hashes are the point: they are pinned to the paper's bytes by
    ``tests/test_memebench_meme_official_judge.py``, so a stamped run whose hash
    differs was scored with a prompt that is not MEME's.
    """
    return {
        "criterion": "meme_official_figure24",
        "judge_model": judge_model,
        "judge_temperature": JUDGE_TEMPERATURE,
        "judge_max_tokens": JUDGE_MAX_TOKENS,
        "provider": provider,
        "abs_judge_prompt_sha256": sha256_text(ABS_JUDGE_PROMPT),
        "before_judge_prompt_sha256": sha256_text(BEFORE_JUDGE_PROMPT),
        "input_runs": {run: str(path) for run, path in sorted(runs.items())},
        "n_calls": len(plan),
        "plan_sha256": hashlib.sha256(
            json.dumps(
                [
                    [c.key, sha256_text(c.prompt)]
                    for c in sorted(plan, key=lambda c: c.key)
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "artifacts_mutated": False,
        "generation_rerun": False,
    }


def usage_attribution_audit(
    rows: Sequence[Mapping[str, Any]],
    client_totals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Check that per-call usage deltas sum to the clients' own totals.

    ``CountingChatClient`` counts into shared mutable fields, so reading a
    before/after delta around an ``await`` attributes correctly ONLY when nothing
    else is using that client concurrently. Run four calls through one client and
    each call's "after" snapshot absorbs the others' tokens: totals inflate and
    per-call numbers become meaningless (measured 2026-09-02: a 221-token prompt
    was recorded as 2201). The runner therefore gives each concurrency slot its
    own client, and this audit is what proves it -- a mismatch means the cost
    table is wrong, so the run fails closed rather than reporting inflated spend.

    Only calls made in the current process can be audited; resumed rows were
    counted by a previous process whose clients are gone.
    """
    per_call = {
        field: sum(int((r.get("usage") or {}).get(field) or 0) for r in rows)
        for field in ("prompt_tokens", "completion_tokens", "calls")
    }
    clients = {
        field: sum(int(t.get(field) or 0) for t in client_totals)
        for field in ("prompt_tokens", "completion_tokens", "calls")
    }
    return {
        "per_call_sum": per_call,
        "client_totals": clients,
        "ok": per_call == clients,
        "n_rows_audited": len(rows),
        "n_clients": len(client_totals),
    }


def usd_cost(
    prompt_tokens: int, completion_tokens: int, price: Mapping[str, Any]
) -> float:
    return (
        prompt_tokens / 1e6 * float(price["input_per_million"])
        + completion_tokens / 1e6 * float(price["output_per_million"])
    )


# --- checkpoint ----------------------------------------------------------------


def read_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    """Completed calls from a prior attempt, keyed by call key.

    A rejudge is 357 paid calls; a crash at call 300 must not re-spend the first
    299. Only rows carrying a raw output are kept, so a partially written line
    cannot masquerade as a completed grading.
    """
    if not path.exists():
        return {}
    done: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated tail from a hard kill
        if isinstance(row, dict) and row.get("key") and "raw_output" in row:
            done[str(row["key"])] = row
    return done


def verdicts_from_rows(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, OfficialVerdict]:
    """Re-parse checkpointed raw outputs, so parsing is never trusted from disk."""
    return {key: parse_verdict(str(row.get("raw_output") or "")) for key, row in rows.items()}
