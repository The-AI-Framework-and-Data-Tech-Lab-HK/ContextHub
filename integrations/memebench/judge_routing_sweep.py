"""Task (d) — propagation-side judge routing on the BIPOLAR edge set.

Stage A ran four judge tiers on gold edges, but gold edges are single-polarity
(all should-stale), so it could only measure RECALL — the curve degenerated to
"hop-1 free rule suffices / hop-2 cheap LLM suffices", no routing room. This
script closes that gap: it runs the same four tiers on the BIPOLAR edge set from
① (`runs/neg_edge_set_hop{1,2}.json` — positive should-stale edges + negative
should-NOT-stale traps), so we get BOTH recall (positives caught) AND precision
(negatives not over-marked), i.e. a real cost×quality Pareto for the propagation
judge.

Scoring convention (edge-local, matches ①'s root-relative labels):
  positive edge (should_stale=true)  judged stale     -> TP
  positive edge                      judged not-stale  -> FN  (missed cascade — worst)
  negative edge (should_stale=false) judged stale      -> FP  (over-marking)
  negative edge                      judged not-stale  -> TN
  recall    = TP / (TP + FN)   — soundness: don't miss real staleness
  precision = TP / (TP + FP)   — don't over-mark false edges

Change source (the key design knob, --change-source):
  "upstream" (default): feed the edge's DIRECT upstream (dependency_text) as the
      thing that changed, and ask if the dependent's stated value goes stale.
      This tests whether the EDGE is a real derivation. It is the only faithful
      option: 768/809 semantic-negative edges have a non-root upstream (chatter
      nodes like "switched to a scooter"), so feeding the root change instead
      would make those negatives trivially rejectable by even a free judge and
      collapse the routing room. Also matches Stage A's finding that hop-2 edges
      judged against the root (not the direct upstream) get J1 recall=0.
  "root": feed the root before->after change to every edge (ablation / sanity).

The four tiers (all consume two texts only — route 1, no DB, no graph rebuild):
  J1 rule       : dependency content words appear in dependent text, AND the
                  dependent is not a predeclaration ("^if ... will/would").
  J2 embedding  : cosine(dependency_text, dependent_text) >= threshold.
  J3 cheap LLM  : edge-local staleness prompt on a cheap model.
  J4 costly LLM : same prompt on a costly model.
J3/J4 are pluggable (`_llm_verdict`); a future --j34-backend=oracle can swap in
the live DerivedMemoryOracleRule (needs DB node rebuild — route 2, not here).

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.judge_routing_sweep \
        --edges integrations/memebench/runs/neg_edge_set_hop1.json \
        --cheap-model gpt-4o-mini --costly-model claude-opus-4-8 --provider openlux \
        --out integrations/memebench/runs/judge_routing_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.systems import (
    DEFAULT_PROVIDERS_PATH,
    build_system,
    load_provider,
)

EMBED_THRESHOLDS = (0.30, 0.40, 0.50, 0.60, 0.70)

# Predeclaration signature (same family as the core _CONDITIONAL_RE): a leading
# "if ... will/would/becomes/changes" main clause. Such a dependent asserts a
# future rule, not a present derived value, so it should NOT go stale.
_PREDECL_RE = re.compile(r"^\s*if\b.{0,80}?\b(will|would|becomes?|changes?|switch(?:es)?)\b",
                         re.IGNORECASE | re.DOTALL)

_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "and", "or", "but", "if", "then", "this",
    "that", "these", "those", "it", "its", "as", "with", "by", "from", "will",
    "would", "my", "i", "you", "he", "she", "they", "we", "user", "s", "their",
    "has", "have", "had", "which", "would", "likely", "change", "changes",
}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 2}


def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_LLM_PROMPT = """You judge whether a stored derived fact has become stale after an \
upstream change.

What actually changed (the root fact):
{change}

A note that this derived fact was recorded as depending on:
{upstream}

The derived note currently states:
{dependent}

Question: given the root change above, is the derived note's stated value now \
incorrect or outdated? It is stale ONLY if its value was genuinely computed from \
what changed. A note that merely shares a topic with the upstream, restates a \
future rule ("if X changes, Y will be ..."), or is an unrelated opinion/activity \
is NOT stale (even if it mentions the upstream).
Answer with exactly one word on the first line: YES or NO. Then one short reason."""


@dataclass
class EdgeVerdict:
    should_stale: bool
    neg_class: str
    j1_rule: bool
    j2_cosine: float
    j3_cheap: bool | None = None
    j4_costly: bool | None = None
    j3_tokens: int = 0
    j4_tokens: int = 0
    error: str | None = None


async def _llm_verdict(chat: CountingChatClient, change: str, upstream: str,
                       dependent: str) -> tuple[bool, int]:
    t0 = chat.total_tokens
    prompt = _LLM_PROMPT.format(change=change, upstream=upstream, dependent=dependent)
    answer = await chat.complete(prompt, max_tokens=60)
    tok = chat.total_tokens - t0
    return (answer or "").strip().upper().startswith("YES"), tok


def _root_change_text(edge: dict) -> str:
    return (f"The '{edge.get('root_entity','')}' changed from "
            f"'{edge.get('root_before','')}' to '{edge.get('root_after','')}'.")


def _upstream_text(edge: dict, change_source: str) -> str:
    """The 'what changed' text fed to every judge for this edge."""
    if change_source == "root":
        return (f"An upstream fact changed from '{edge.get('root_before','')}' "
                f"to '{edge.get('root_after','')}'.")
    return edge["dependency_text"]  # edge-local: the direct upstream


def judge_j1(upstream: str, dependent: str) -> bool:
    """Free rule: dependent shares upstream content words AND is not a predeclaration."""
    if _PREDECL_RE.search(dependent or ""):
        return False
    up_w = _content_words(upstream)
    dep_w = _content_words(dependent)
    return bool(up_w & dep_w)


@dataclass
class JudgeStats:
    name: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    tokens: int = 0

    def add(self, pred_stale: bool, gold_stale: bool) -> None:
        if gold_stale and pred_stale:
            self.tp += 1
        elif gold_stale and not pred_stale:
            self.fn += 1
        elif not gold_stale and pred_stale:
            self.fp += 1
        else:
            self.tn += 1

    def finalize(self, n_edges: int) -> dict:
        prec = self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None
        rec = self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None
        f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
        return {
            "name": self.name,
            "precision": prec, "recall": rec, "f1": f1,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "tokens_per_edge": (self.tokens / n_edges) if n_edges else 0.0,
        }


async def run(edges: list[dict], embed_map: dict[str, list], cheap_chat, costly_chat,
              change_source: str, thresholds) -> dict:
    verdicts: list[EdgeVerdict] = []
    j1 = JudgeStats("J1_rule")
    j2 = {t: JudgeStats(f"J2_embed@{t}") for t in thresholds}
    j3 = JudgeStats("J3_cheap_llm")
    j4 = JudgeStats("J4_costly_llm")

    n = len(edges)
    for i, edge in enumerate(edges, 1):
        gold = bool(edge["should_stale"])
        upstream = _upstream_text(edge, change_source)
        dependent = edge["dependent_text"]

        v_j1 = judge_j1(upstream, dependent)
        cos = _cosine(embed_map.get(upstream), embed_map.get(dependent))
        rec = EdgeVerdict(should_stale=gold, neg_class=edge.get("neg_class", ""),
                          j1_rule=v_j1, j2_cosine=round(cos, 4))
        j1.add(v_j1, gold)
        for t in thresholds:
            j2[t].add(cos >= t, gold)

        change = _root_change_text(edge)
        try:
            v_j3, tok3 = await _llm_verdict(cheap_chat, change, upstream, dependent)
            rec.j3_cheap, rec.j3_tokens = v_j3, tok3
            j3.add(v_j3, gold); j3.tokens += tok3
            v_j4, tok4 = await _llm_verdict(costly_chat, change, upstream, dependent)
            rec.j4_costly, rec.j4_tokens = v_j4, tok4
            j4.add(v_j4, gold); j4.tokens += tok4
        except Exception as exc:
            rec.error = f"{type(exc).__name__}: {exc}"
        verdicts.append(rec)
        if i % 50 == 0 or i == n:
            print(f"  [{i}/{n}] judged", flush=True)

    # best J2 threshold by F1 (for the headline curve)
    j2_final = [s.finalize(n) for s in j2.values()]
    j2_best = max((r for r in j2_final if r["f1"] is not None),
                  key=lambda r: r["f1"], default=j2_final[0])
    return {
        "judges": [j1.finalize(n), j2_best, j3.finalize(n), j4.finalize(n)],
        "j2_all_thresholds": j2_final,
        "verdicts": [asdict(v) for v in verdicts],
    }


def _attach_root(edges: list[dict], data_path: str) -> None:
    """Fill root_entity/before/after per edge from the MEME episode.

    Always run: the LLM tiers (J3/J4) need the root before->after to judge whether
    the derived value changed. J1/J2 stay edge-local and ignore it — that gap (cheap
    tiers can't reason about 'what it changed to') is exactly the routing room (d) tests.
    """
    from integrations.memebench.loader import load_episodes
    root = {}
    for e in load_episodes(data_path):
        rc = e.get("root_change", {})
        root[e["episode_id"]] = (e.get("root"), rc.get("before"), rc.get("after"))
    for edge in edges:
        ent, b, a = root.get(edge["episode_id"], (None, None, None))
        edge["root_entity"], edge["root_before"], edge["root_after"] = ent, b, a


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edges", required=True, help="neg_edge_set_hop{1,2}.json")
    ap.add_argument("--data", default="/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json",
                    help="MEME episodes, for root before->after lookup")
    ap.add_argument("--limit", type=int, default=0, help="max edges (0 = all)")
    ap.add_argument("--change-source", choices=["upstream", "root"], default="upstream")
    ap.add_argument("--provider", default="openlux")
    ap.add_argument("--cheap-model", default="gpt-4o-mini")
    ap.add_argument("--costly-model", default="claude-opus-4-8")
    ap.add_argument("--out", default="integrations/memebench/runs/judge_routing.json")
    args = ap.parse_args()

    edges = json.load(open(args.edges, encoding="utf-8"))["edges"]
    if args.limit:
        edges = edges[: args.limit]
    _attach_root(edges, args.data)

    system = await build_system(provider_label=args.provider)
    prov = load_provider(args.provider, DEFAULT_PROVIDERS_PATH)
    cheap_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=args.cheap_model))
    costly_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=args.costly_model))

    # Batch-embed every unique text once (upstream + dependent across all edges).
    texts = set()
    for e in edges:
        texts.add(_upstream_text(e, args.change_source))
        texts.add(e["dependent_text"])
    texts = [t for t in texts if t]
    print(f"embedding {len(texts)} unique texts ...", flush=True)
    embs = await system.embedding.embed_batch(texts)
    embed_map = {t: v for t, v in zip(texts, embs)}

    try:
        result = await run(edges, embed_map, cheap_chat, costly_chat,
                           args.change_source, EMBED_THRESHOLDS)
    finally:
        await cheap_chat.close()
        await costly_chat.close()
        await system.close()

    n_err = sum(1 for v in result["verdicts"] if v["error"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "edges_file": args.edges, "n_edges": len(edges), "n_error": n_err,
        "change_source": args.change_source,
        "cheap_model": args.cheap_model, "costly_model": args.costly_model,
        "judges": result["judges"], "j2_all_thresholds": result["j2_all_thresholds"],
        "verdicts": result["verdicts"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"Judge routing on bipolar edges  ({len(edges)} edges, "
          f"change_source={args.change_source}, {n_err} err)")
    print("=" * 74)
    print(f"{'judge':<16}{'precision':>10}{'recall':>9}{'f1':>7}{'tok/edge':>10}"
          f"  (TP/FP/FN/TN)")
    for j in result["judges"]:
        p = f"{j['precision']:.3f}" if j['precision'] is not None else "  -  "
        r = f"{j['recall']:.3f}" if j['recall'] is not None else "  -  "
        f = f"{j['f1']:.3f}" if j['f1'] is not None else "  -  "
        print(f"{j['name']:<16}{p:>10}{r:>9}{f:>7}{j['tokens_per_edge']:>10.0f}"
              f"  ({j['tp']}/{j['fp']}/{j['fn']}/{j['tn']})")
    print(f"\nArtifacts -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
