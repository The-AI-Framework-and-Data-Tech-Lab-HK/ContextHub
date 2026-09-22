"""Pair two `Abs` runs episode by episode: with notices vs without.

Zero API. Reads two frozen run directories and reports, in this order:

1. **Noise floor.** The `before` and `off` arms are configured identically in both
   runs, so any difference between them is run-to-run variation, not an effect.
   ``chat_client`` does not send a temperature unless asked, so the provider
   default applies and repeat answers are not guaranteed identical. Every other
   number here must be read against this one: a gap no larger than the noise
   floor is not evidence of anything.

2. **The decomposition.** With notices ON the `on` arm both withholds the stale
   note *and* explains it in a different prompt template; with notices OFF it
   withholds without explaining, on the same template as `off`. So:

       B.off -> B.on   only propagation differs (template held fixed)  -- clean
       B.on  -> A.on   only template+notices differ (propagation held) -- packed
       A.off -> A.on   both differ at once                             -- the
                       published total, which is why it cannot be called a
                       causal contribution of propagation on its own

   The middle row stays packed: "the notice content" and "the prompt spelling out
   the answer format" move together and this pair of runs cannot separate them.

Stratification uses the *notices* run's labels for both runs. A no-notices run has
zero notices on every episode by construction, so deriving `signal_on` from it
would label every episode as having no propagation signal.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

ARMS = ("before", "off", "on")
THREE_PART = ("abstained", "cited_prev", "named_upstream")


class CompareError(RuntimeError):
    """Raised when the two runs cannot be paired."""


def load_run(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Full-run artifacts keyed by episode id."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(glob.glob(str(run_dir / "artifacts" / "full-run" / "*" / "artifact.json"))):
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
        episode_id = str(artifact.get("episode_id"))
        if episode_id in out:
            raise CompareError(f"{run_dir.name}: duplicate episode {episode_id}")
        out[episode_id] = artifact
    if not out:
        raise CompareError(f"{run_dir}: no full-run artifacts")
    return out


def notices_flag(artifacts: Mapping[str, Mapping[str, Any]], label: str) -> bool:
    """The run's recorded with_stale_notices, refusing to guess when absent."""
    values = set()
    for episode_id, artifact in artifacts.items():
        if "with_stale_notices" not in artifact:
            raise CompareError(f"{label}/{episode_id}: no with_stale_notices recorded")
        values.add(bool(artifact["with_stale_notices"]))
    if len(values) != 1:
        raise CompareError(f"{label}: run mixes with_stale_notices values")
    return values.pop()


def answer(artifact: Mapping[str, Any], arm: str) -> str:
    return str((((artifact.get("answers") or {}).get(arm)) or {}).get("raw_answer") or "")


def three_part(artifact: Mapping[str, Any], arm: str) -> dict[str, bool]:
    stages = ((artifact.get("judge") or {}).get("abs_scoring") or {}).get("stages") or {}
    row = stages.get(arm) or {}
    return {name: row.get(name) is True for name in (*THREE_PART, "correct")}


def notice_count(artifact: Mapping[str, Any], arm: str = "on") -> int:
    retrieval = (((artifact.get("answers") or {}).get(arm)) or {}).get("retrieval") or {}
    return len(retrieval.get("stale_notices") or [])


def noise_floor(
    notices_run: Mapping[str, Mapping[str, Any]],
    plain_run: Mapping[str, Mapping[str, Any]],
    shared: Sequence[str],
) -> dict[str, Any]:
    """How much the identically-configured arms moved between the two runs.

    ``before`` and ``off`` receive the same prompt in both runs, so a difference
    is run-to-run variation. Reported as exact-string agreement and as the count
    whose three-part verdict flipped: the second is what matters, since a reworded
    answer that scores the same does not move any reported number.
    """
    out: dict[str, Any] = {}
    for arm in ("before", "off"):
        same_text = sum(
            1 for e in shared if answer(notices_run[e], arm) == answer(plain_run[e], arm)
        )
        flipped = [
            e
            for e in shared
            if three_part(notices_run[e], arm)["correct"]
            != three_part(plain_run[e], arm)["correct"]
        ]
        out[arm] = {
            "n": len(shared),
            "identical_text": same_text,
            "identical_text_pct": round(100 * same_text / len(shared), 1) if shared else None,
            "verdict_flipped": len(flipped),
            "verdict_flipped_episodes": sorted(flipped),
        }
    out["max_verdict_flips"] = max(out[a]["verdict_flipped"] for a in ("before", "off"))
    return out


def arm_rates(
    run: Mapping[str, Mapping[str, Any]], episodes: Sequence[str], arm: str
) -> dict[str, Any]:
    """Three-part hit counts for one arm over a fixed episode set."""
    rows = [three_part(run[e], arm) for e in episodes]
    return {
        "n": len(rows),
        **{name: sum(1 for r in rows if r[name]) for name in THREE_PART},
        "all_three": sum(1 for r in rows if r["correct"]),
    }


def compare(
    notices_dir: Path, plain_dir: Path, *, strict_flags: bool = True
) -> dict[str, Any]:
    """The full paired comparison. Raises if the runs are not comparable."""
    notices_run = load_run(notices_dir)
    plain_run = load_run(plain_dir)

    if strict_flags:
        if not notices_flag(notices_run, notices_dir.name):
            raise CompareError(f"{notices_dir.name} was run WITHOUT notices")
        if notices_flag(plain_run, plain_dir.name):
            raise CompareError(f"{plain_dir.name} was run WITH notices")

    shared = sorted(set(notices_run) & set(plain_run))
    if not shared:
        raise CompareError("the two runs share no episodes")
    only_notices = sorted(set(notices_run) - set(plain_run))
    only_plain = sorted(set(plain_run) - set(notices_run))

    # Strata come from the notices run for BOTH runs: see module docstring.
    labels = {
        "all": shared,
        "domain_pl": [e for e in shared if e.startswith("pl")],
        "domain_sw": [e for e in shared if e.startswith("sw")],
        "signal_on": [e for e in shared if notice_count(notices_run[e]) > 0],
        "no_signal_on": [e for e in shared if notice_count(notices_run[e]) == 0],
    }

    strata: dict[str, Any] = {}
    for name, episodes in labels.items():
        if not episodes:
            strata[name] = {"n": 0}
            continue
        strata[name] = {
            "n": len(episodes),
            "episode_ids": list(episodes),
            "A_off": arm_rates(notices_run, episodes, "off"),
            "A_on": arm_rates(notices_run, episodes, "on"),
            "B_off": arm_rates(plain_run, episodes, "off"),
            "B_on": arm_rates(plain_run, episodes, "on"),
        }

    return {
        "criterion": "three_part_runner_judge",
        "note": (
            "Three-part is our own stricter criterion and is an ADDITIONAL view. "
            "The headline criterion is MEME's official judge, which requires only "
            "that uncertainty be expressed; run the rejudge for those numbers. "
            "named_upstream is near-uninformative in the no-notices run because the "
            "simple template never asks for that format."
        ),
        "runs": {
            "A_with_notices": str(notices_dir),
            "B_no_notices": str(plain_dir),
        },
        "pairing": {
            "shared": len(shared),
            "only_in_A": only_notices,
            "only_in_B": only_plain,
        },
        "noise_floor": noise_floor(notices_run, plain_run, shared),
        "strata": strata,
        "per_episode": [
            {
                "episode_id": e,
                "notice_count_A": notice_count(notices_run[e]),
                "A_off_answer": answer(notices_run[e], "off"),
                "A_on_answer": answer(notices_run[e], "on"),
                "B_off_answer": answer(plain_run[e], "off"),
                "B_on_answer": answer(plain_run[e], "on"),
                "A_on_all_three": three_part(notices_run[e], "on")["correct"],
                "B_on_all_three": three_part(plain_run[e], "on")["correct"],
            }
            for e in shared
        ],
    }


def _fmt(part: int, whole: int) -> str:
    return f"{part:3d}/{whole:<3d} ({part / whole * 100:5.1f}%)" if whole else "   n/a"


def print_report(report: Mapping[str, Any]) -> None:
    nf = report["noise_floor"]
    print(f"\n{'=' * 74}\nNOISE FLOOR  (same prompt in both runs -- differences are variation)\n{'=' * 74}")
    for arm in ("before", "off"):
        row = nf[arm]
        print(
            f"  {arm:7s} identical text {_fmt(row['identical_text'], row['n'])}   "
            f"verdict flips: {row['verdict_flipped']}"
        )
    print(f"\n  => read every gap below against a noise floor of {nf['max_verdict_flips']} flipped verdicts")

    for name, block in report["strata"].items():
        if not block.get("n"):
            continue
        n = block["n"]
        print(f"\n{'=' * 74}\n[{name}]  n={n}\n{'=' * 74}")
        print(f"  {'arm':22s} {'abstained':>16s} {'cited_prev':>16s} {'all three':>16s}")
        for key, label in (
            ("B_off", "B off (no prop)"),
            ("B_on", "B on  (prop, no notice)"),
            ("A_on", "A on  (prop + notice)"),
        ):
            row = block[key]
            print(
                f"  {label:22s} {_fmt(row['abstained'], n):>16s} "
                f"{_fmt(row['cited_prev'], n):>16s} {_fmt(row['all_three'], n):>16s}"
            )
        b_off, b_on, a_on = block["B_off"], block["B_on"], block["A_on"]
        print(
            f"\n  propagation alone  (B.off -> B.on): abstained "
            f"{b_on['abstained'] - b_off['abstained']:+d}"
        )
        print(
            f"  template+notices   (B.on  -> A.on): abstained "
            f"{a_on['abstained'] - b_on['abstained']:+d}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notices-run", type=Path, required=True)
    parser.add_argument("--no-notices-run", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--allow-flag-mismatch",
        action="store_true",
        help="skip the with_stale_notices check (for inspecting two same-flag runs)",
    )
    args = parser.parse_args()

    report = compare(
        args.notices_run, args.no_notices_run, strict_flags=not args.allow_flag_mismatch
    )
    print_report(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
