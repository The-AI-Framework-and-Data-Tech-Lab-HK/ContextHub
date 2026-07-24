"""Probe: is mode-B free extraction good enough to rebuild ingest on raw dialogue?

Feasibility check BEFORE any full rebuild. For N cascade cases it feeds the raw
evidence-session conversations to ConversationExtractionService (opus, no entity
list, never sees gold) and scores three things against gold — nothing touches
the DB, retrieval, or propagation:

  (a) extraction recall  : did the extractor capture the values that matter?
      - root current value (before)
      - each cascade target's predeclaration value (the after answer)
      - each cascade target's materialized current value (before)
  (b) entity alignment   : of gold cascade entities, how many have their value
      recoverable from some extracted line (proxy for "can we still build the
      dependency graph without gold entity names").
  (c) syntactic-rule hit : does the hard-mode _CONDITIONAL_RE still fire on the
      extracted predeclaration line? (risk #3: colloquial extraction may lose the
      "if ... will/would" shape the hard rule depends on.)

Usage:
  CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -u -m integrations.memebench.extraction_probe \
      --hop 1 --limit 10 --extract-model claude-opus-4-8 --out integrations/memebench/runs/probe
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import _CONDITIONAL_RE

from integrations.memebench.cost import CountingChatClient
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import load_provider

DEFAULT_DATA = str(DEFAULT_DATA_PATH)


def _norm(s: str) -> str:
    return " ".join(str(s or "").casefold().split())


def _value_recalled(value, extracted_norm: list[str]) -> bool:
    """A gold value counts as recalled if it appears in any extracted line."""
    v = _norm(value)
    if not v:
        return False
    return any(v in line for line in extracted_norm)


def _gold_facts_for(case, entity):
    return [
        g
        for sess in case.sessions
        for g in sess.get("gold_facts", [])
        if g.get("entity") == entity
    ]


def _session_text(sess) -> str:
    parts: list[str] = []
    for turn in sess.get("conversation", []):
        role = turn.get("role", "")
        content = turn.get("content", "")
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _evidence_sessions(case) -> list[dict]:
    return [s for s in case.sessions if s.get("type") != "filler"]


def _evidence_conversation(case) -> str:
    """Concatenate the evidence sessions' turns into one transcript (no filler)."""
    return "\n".join(_session_text(s) for s in _evidence_sessions(case))


async def probe_case(
    extractor: ConversationExtractionService, case, *, incremental: bool = False
) -> dict:
    """Extract facts from a case's evidence conversation.

    incremental=True extracts each evidence session separately and unions the
    facts — mirroring the real timeline ingest, so a before-value stated in an
    early session is captured before a later change session overwrites it.
    incremental=False (default) feeds the whole transcript at once (a memory
    system then rationally keeps only the latest value, dropping stale ones).
    """
    if incremental:
        facts = []
        for sess in _evidence_sessions(case):
            facts.extend(await extractor.extract(_session_text(sess)))
    else:
        facts = await extractor.extract(_evidence_conversation(case))
    extracted_norm = [_norm(f.text) for f in facts]

    # Gold targets to check: root + each cascade target.
    root_before = case.entities.get(case.root).before if case.entities.get(case.root) else case.root_change.get("before")
    checks = {"root_before": _value_recalled(root_before, extracted_norm)}

    target_recall = []   # (entity, pre_value_recalled, cur_value_recalled, syntactic_hit)
    for edge in case.edges:
        target = edge.target
        ent = case.entities.get(target)
        if not ent:
            continue
        after_val = ent.after
        before_val = ent.before
        pre_ok = _value_recalled(after_val, extracted_norm) if after_val else None
        cur_ok = _value_recalled(before_val, extracted_norm) if before_val else None
        # syntactic hit: does any extracted line carrying the after value also
        # look conditional (matches the hard-mode regex)?
        syn_hit = None
        if after_val:
            av = _norm(after_val)
            lines_with_after = [f.text for f in facts if av in _norm(f.text)]
            syn_hit = any(bool(_CONDITIONAL_RE.search(t)) for t in lines_with_after)
        target_recall.append({
            "entity": target,
            "after_value": after_val,
            "pre_value_recalled": pre_ok,
            "cur_value_recalled": cur_ok,
            "predecl_syntactic_hit": syn_hit,
        })

    return {
        "episode_id": case.episode_id,
        "hop": case.hop,
        "n_extracted": len(facts),
        "root_before_recalled": checks["root_before"],
        "targets": target_recall,
        "extracted_facts": [f.text for f in facts],
    }


def _aggregate(rows: list[dict]) -> dict:
    root_hits = sum(1 for r in rows if r["root_before_recalled"])
    pre_tot = pre_hit = cur_tot = cur_hit = syn_tot = syn_hit = 0
    for r in rows:
        for t in r["targets"]:
            if t["pre_value_recalled"] is not None:
                pre_tot += 1
                pre_hit += int(t["pre_value_recalled"])
            if t["cur_value_recalled"] is not None:
                cur_tot += 1
                cur_hit += int(t["cur_value_recalled"])
            if t["predecl_syntactic_hit"] is not None:
                syn_tot += 1
                syn_hit += int(t["predecl_syntactic_hit"])
    div = lambda a, b: (a / b) if b else None
    return {
        "n_cases": len(rows),
        "root_before_recall": div(root_hits, len(rows)),
        "predecl_value_recall": div(pre_hit, pre_tot),
        "materialized_value_recall": div(cur_hit, cur_tot),
        "predecl_syntactic_hit_rate": div(syn_hit, syn_tot),
        "counts": {
            "root": [root_hits, len(rows)],
            "predecl_value": [pre_hit, pre_tot],
            "materialized_value": [cur_hit, cur_tot],
            "predecl_syntactic_hit": [syn_hit, syn_tot],
        },
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--hop", type=int, choices=[1, 2], default=None)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--sample", type=int, default=None,
                    help="randomly sample this many cases (reproducible via --seed) "
                         "instead of taking the first --limit")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --sample")
    ap.add_argument("--extract-model", default="claude-opus-4-8")
    ap.add_argument("--incremental", action="store_true",
                    help="extract each evidence session separately and union facts "
                         "(mirrors timeline ingest; before-values survive later changes)")
    ap.add_argument("--provider-label", default="yunwu")
    ap.add_argument("--out", default="integrations/memebench/runs/probe")
    args = ap.parse_args()

    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.sample:
        import random
        rng = random.Random(args.seed)
        cases = rng.sample(cases, min(args.sample, len(cases)))
    elif args.limit:
        cases = cases[: args.limit]
    mode = "incremental" if args.incremental else "whole-transcript"
    print(f"Probe: {len(cases)} cases (hop={args.hop or 'all'}) extractor={args.extract_model} mode={mode}", flush=True)

    provider = load_provider(args.provider_label)
    chat = CountingChatClient(
        OpenAIChatClient(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            model=args.extract_model,
        )
    )
    extractor = ConversationExtractionService(chat)

    rows = []
    for i, case in enumerate(cases, 1):
        row = await probe_case(extractor, case, incremental=args.incremental)
        rows.append(row)
        print(f"  [{i}/{len(cases)}] {row['episode_id']} extracted={row['n_extracted']} "
              f"root={row['root_before_recalled']}", flush=True)

    agg = _aggregate(rows)
    snap = chat.snapshot() if hasattr(chat, "snapshot") else {}

    print("\n=== extraction probe (mode B, no entity schema) ===", flush=True)
    print(f"cases={agg['n_cases']} extractor={args.extract_model}", flush=True)
    print(f"root before-value recall     : {agg['root_before_recall']}  {agg['counts']['root']}", flush=True)
    print(f"predeclaration value recall  : {agg['predecl_value_recall']}  {agg['counts']['predecl_value']}", flush=True)
    print(f"materialized value recall    : {agg['materialized_value_recall']}  {agg['counts']['materialized_value']}", flush=True)
    print(f"predecl syntactic-hit rate   : {agg['predecl_syntactic_hit_rate']}  {agg['counts']['predecl_syntactic_hit']}", flush=True)
    print(f"extractor tokens             : {snap}", flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe_cases.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "probe_summary.json").write_text(
        json.dumps({"aggregate": agg, "extractor_tokens": snap, "extract_model": args.extract_model},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nArtifacts written to {out_dir}/", flush=True)
    await chat.close()


if __name__ == "__main__":
    asyncio.run(main())
