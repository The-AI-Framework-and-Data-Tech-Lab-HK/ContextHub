"""Aggregate online-proxy decision logs into observe-phase readouts.

The online proxy (``online_proxy`` / ``online_handoff_proxy``) emits one
``DecisionRecord`` per gated boundary. In ``observe`` mode the proxy always
forwards, so the JSONL it produces is purely a measurement artifact. This module
turns that JSONL into the P0-2 deliverable: would-block / would-repair rates and
a per-(agent, tool) breakdown.

Honest scope: this aggregates *what the gate would have done*. The **absolute
false-block** number (a would-block on a case that S0 actually passes) requires
joining these records with S0 task-pass results, which lives in P1-7. Here we
only surface ``would_block`` / ``would_repair`` counts and rates.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import argparse
import json
from pathlib import Path
from typing import Any


_BLOCK_ACTIONS = frozenset({"block", "pending", "retry_with_feedback"})
_REPAIR_ACTIONS = frozenset({"retry_with_patch"})


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Read a decision-log JSONL file into a list of record dicts."""

    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _as_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    to_json = getattr(record, "to_json", None)
    if callable(to_json):
        return dict(to_json())
    raise TypeError(f"cannot coerce {type(record)!r} into a decision record dict")


def _label(record: Mapping[str, Any]) -> str:
    agent = str(record.get("agent_id") or "")
    # Tool records carry tool_name; handoff records carry recipient.
    target = str(record.get("tool_name") or record.get("recipient") or "")
    return f"{agent}.{target}" if target else agent


def summarize(records: Iterable[Any]) -> dict[str, Any]:
    """Aggregate decision records into observe-phase counts and rates.

    Accepts either record dicts (JSONL form) or objects exposing ``to_json``.
    """

    rows = [_as_dict(record) for record in records]
    total = len(rows)

    by_mode: Counter[str] = Counter()
    by_verdict: Counter[str] = Counter()
    by_action: Counter[str] = Counter()
    by_schema_source: Counter[str] = Counter()
    would_block = 0
    would_repair = 0
    forwarded = 0
    patched = 0
    errors = 0
    per_target: dict[str, dict[str, Any]] = {}

    for row in rows:
        mode = str(row.get("mode") or "")
        verdict = str(row.get("verdict") or "")
        action = str(row.get("action") or "")
        by_mode[mode] += 1
        by_verdict[verdict] += 1
        by_action[action] += 1
        by_schema_source[str(row.get("schema_source") or "")] += 1

        # Prefer explicit flags when present; fall back to action classification
        # so logs from either proxy (tool/handoff) aggregate uniformly.
        is_block = bool(row.get("would_block")) or action in _BLOCK_ACTIONS
        is_repair = bool(row.get("would_repair")) or action in _REPAIR_ACTIONS
        would_block += int(is_block)
        would_repair += int(is_repair)
        forwarded += int(bool(row.get("forwarded")))
        patched += int(bool(row.get("patched")))
        if verdict == "error" or row.get("error"):
            errors += 1

        label = _label(row)
        cell = per_target.setdefault(
            label,
            {"count": 0, "would_block": 0, "would_repair": 0, "verdicts": Counter()},
        )
        cell["count"] += 1
        cell["would_block"] += int(is_block)
        cell["would_repair"] += int(is_repair)
        cell["verdicts"][verdict] += 1

    return {
        "total": total,
        "by_mode": dict(sorted(by_mode.items())),
        "by_verdict": dict(sorted(by_verdict.items())),
        "by_action": dict(sorted(by_action.items())),
        "by_schema_source": dict(sorted(by_schema_source.items())),
        "would_block": would_block,
        "would_repair": would_repair,
        "forwarded": forwarded,
        "patched": patched,
        "errors": errors,
        "would_block_rate": _rate(would_block, total),
        "would_repair_rate": _rate(would_repair, total),
        "per_target": {
            label: {
                "count": cell["count"],
                "would_block": cell["would_block"],
                "would_repair": cell["would_repair"],
                "verdicts": dict(sorted(cell["verdicts"].items())),
            }
            for label, cell in sorted(per_target.items())
        },
        "note": (
            "would_block/would_repair are gate intentions only. Absolute "
            "false-block requires joining with S0 task-pass results (P1-7)."
        ),
    }


def summarize_file(path: str | Path) -> dict[str, Any]:
    """Convenience wrapper: load a JSONL decision log and summarize it."""

    summary = summarize(load_records(path))
    summary["source"] = str(path)
    return summary


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="decision-log JSONL produced by the online proxy")
    parser.add_argument("--out", type=Path, default=None, help="optional JSON summary output path")
    args = parser.parse_args(argv)

    summary = summarize_file(args.log)
    text = json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
