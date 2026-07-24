"""Aggregate CascadeCase results into the comparison table + cost, write artifacts.

Reports (per plan):
- Internal ablation: acc(ON) - acc(OFF) = delta, cleanly attributed to the
  propagation layer (everything else held constant).
- By-hop breakdown (1-hop / 2-hop).
- Cost: oracle calls + est. tokens.
- Static MEME baseline row (6 systems avg 3% on Cascade; MD-flat x Opus4.7
  0.32 @ ~70x) — cited from the paper, NOT rerun.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

# Static figures cited from MEME (arXiv:2605.12477); not rerun here.
MEME_BASELINE = {
    "six_systems_cascade_avg_acc": 0.03,     # 6 systems, gpt-4.1-mini, 100 episodes
    "mdflat_opus47_cascade_acc": 0.32,       # MD-flat x Claude Opus 4.7, 20-ep subset
    "mdflat_opus47_cost_multiple": 70,       # ~70x baseline cost
}


def _acc(results, key, hop=None):
    rs = [r for r in results if r.error is None and (hop is None or r.hop == hop)]
    if not rs:
        return None, 0
    n = len(rs)
    hits = sum(1 for r in rs if getattr(r, key))
    return hits / n, n


def _edge_pr(results, hop=None) -> dict:
    """Micro precision/recall of discovered edges vs gold (Step-1 metric).

    Micro = pool tp/pred/gold across cases, so per-case counts weight naturally.
    Also report the macro (mean of per-case P/R) for reference.
    """
    rs = [r for r in results if r.error is None and (hop is None or r.hop == hop)]
    if not rs:
        return {"n": 0}
    tp = sum(r.edge_n_tp for r in rs)
    pred = sum(r.edge_n_pred for r in rs)
    gold = sum(r.edge_n_gold for r in rs)
    micro_p = tp / pred if pred else None
    micro_r = tp / gold if gold else None
    macro_p = sum(r.edge_precision for r in rs if r.edge_precision is not None) / len(rs)
    macro_r = sum(r.edge_recall for r in rs if r.edge_recall is not None) / len(rs)
    return {
        "n": len(rs),
        "n_tp": tp, "n_pred": pred, "n_gold": gold,
        "micro_precision": micro_p, "micro_recall": micro_r,
        "macro_precision": macro_p, "macro_recall": macro_r,
    }


def summarize(results, answer_snap, oracle_snap, discovery_snap, *,
              model: str, edge_mode: str = "gold",
              extract_snap: dict | None = None, judge_snap: dict | None = None) -> dict:
    n_total = len(results)
    n_err = sum(1 for r in results if r.error)
    n_ok = n_total - n_err

    def block(hop):
        off_tp, n = _acc(results, "off_trivial_pass", hop)
        on_tp, _ = _acc(results, "on_trivial_pass", hop)
        off_raw, _ = _acc(results, "off_after_ok", hop)
        on_raw, _ = _acc(results, "on_after_ok", hop)
        return {
            "n": n,
            "off_trivial_pass": off_tp,
            "on_trivial_pass": on_tp,
            "delta_trivial_pass": (on_tp - off_tp) if (on_tp is not None and off_tp is not None) else None,
            "off_after_raw": off_raw,
            "on_after_raw": on_raw,
            "delta_after_raw": (on_raw - off_raw) if (on_raw is not None and off_raw is not None) else None,
        }

    total_oracle_calls = sum(r.oracle_calls for r in results if r.error is None)
    return {
        "model": model,
        "edge_mode": edge_mode,
        "n_total": n_total,
        "n_ok": n_ok,
        "n_error": n_err,
        "overall": block(None),
        "hop1": block(1),
        "hop2": block(2),
        # Step-1: dependency-discovery quality (only meaningful when discovered).
        "edge_discovery": {
            "overall": _edge_pr(results, None),
            "hop1": _edge_pr(results, 1),
            "hop2": _edge_pr(results, 2),
        },
        "cost": {
            "total_oracle_calls": total_oracle_calls,
            "oracle_calls_per_case": (total_oracle_calls / n_ok) if n_ok else None,
            # token buckets: ingest (discovery) / inference (answer) / oracle /
            # extract (raw-dialogue fact extraction, mode B only) / judge.
            "ingest_llm": discovery_snap,
            "inference_llm": answer_snap,
            "oracle_llm": oracle_snap,
            "extract_llm": extract_snap,
            "judge_llm": judge_snap,
        },
        "meme_baseline_static": MEME_BASELINE,
    }


def _fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def _tok(snap: dict) -> str:
    tag = "real" if snap.get("tokens_are_real") else "est"
    return f"{snap['calls']} calls, {snap['total_tokens']} tok ({tag})"


def print_summary(s: dict) -> None:
    print("\n" + "=" * 62)
    print(f"MEME Cascade External-Control Results  (model={s['model']}, "
          f"edge_mode={s.get('edge_mode', 'gold')})")
    print(f"cases: {s['n_ok']} ok / {s['n_error']} error / {s['n_total']} total")
    print("=" * 62)
    print(f"{'stratum':10s} {'n':>4s} {'OFF(tp)':>9s} {'ON(tp)':>9s} {'delta':>8s}  {'OFF(raw)':>9s} {'ON(raw)':>9s}")
    for name in ("overall", "hop1", "hop2"):
        b = s[name]
        print(f"{name:10s} {b['n']:>4d} {_fmt(b['off_trivial_pass']):>9s} {_fmt(b['on_trivial_pass']):>9s} "
              f"{_fmt(b['delta_trivial_pass']):>8s}  {_fmt(b['off_after_raw']):>9s} {_fmt(b['on_after_raw']):>9s}")
    if str(s.get("edge_mode", "")).startswith("discovered"):
        print("-" * 62)
        print("dependency discovery (Step-1) vs gold edges:")
        for name in ("overall", "hop1", "hop2"):
            e = s["edge_discovery"][name]
            if not e.get("n"):
                continue
            print(f"  {name:8s} P={_fmt(e['micro_precision'])} R={_fmt(e['micro_recall'])} "
                  f"(micro; tp={e['n_tp']}/pred={e['n_pred']}/gold={e['n_gold']}) "
                  f"macroP={_fmt(e['macro_precision'])} macroR={_fmt(e['macro_recall'])}")
    c = s["cost"]
    print("-" * 62)
    print(f"oracle calls: {c['total_oracle_calls']} total, {_fmt(c['oracle_calls_per_case'])}/case")
    print(f"ingest    LLM (discovery): {_tok(c['ingest_llm'])}")
    print(f"inference LLM (answer):    {_tok(c['inference_llm'])}")
    print(f"oracle    LLM (staleness): {_tok(c['oracle_llm'])}")
    if c.get("extract_llm"):
        print(f"extract   LLM (raw-B):     {_tok(c['extract_llm'])}")
    if c.get("judge_llm"):
        print(f"judge     LLM (grading):   {_tok(c['judge_llm'])}")
    m = s["meme_baseline_static"]
    print("-" * 62)
    print(f"MEME baseline (cited): 6-sys Cascade avg={m['six_systems_cascade_avg_acc']}, "
          f"MD-flat×Opus4.7={m['mdflat_opus47_cascade_acc']} @ ~{m['mdflat_opus47_cost_multiple']}×")
    print("=" * 62)


def write_artifacts(out_dir: Path, results, summary: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    rows = [asdict(r) for r in results]
    with (out_dir / "cases.json").open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    if rows:
        with (out_dir / "cases.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
