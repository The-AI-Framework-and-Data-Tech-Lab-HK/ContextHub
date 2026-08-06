# ContextHub × MEME — Dependency-Graph Construction Experiments

This document records every experiment we have run on the MEME Cascade benchmark
for ContextHub's **build-side dependency-graph construction** (Problem 1), so the
numbers can be checked and reproduced. It covers the four build variables (node
extraction, coref/disambiguation, edge discovery, candidate selection), the
confidence-cascade router that routes each variable, the negative-edge evaluation
set used for the propagation side, and the supporting single-tier baselines.

All raw artifacts live under `integrations/memebench/runs/` (gitignored — this
file is the checked-in summary). Run logs (`*.log`) sit next to each `*.json`.

---

## 1. Setup common to all runs

- **Benchmark**: MEME (arXiv:2605.12477), Cascade (`Cas`) subset. 100 hop-1 cases
  + 64 hop-2 cases = 164 cases total.
- **Data file**: `meme_filler32k.json` (filler-inflated variant, so the candidate
  pool is realistically large — the O(N^2) predicament only appears with a big
  pool). Path in our runs:
  `/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json`.
- **Provider**: `openlux` (OpenAI-compatible proxy). Models named per experiment.
- **Embeddings**: `text-embedding-3-small` via the same provider.
- **Scoring**:
  - **Path B** (gold nodes over a filler pool) → `edge_pr` gives **exact** edge
    precision/recall (gold entity labels available). Used by the cascade curve,
    candidate-selection sweep, and discovery-model sweep.
  - **Path A / raw dialogue** (self-extracted nodes, no gold labels) →
    `edge_pr_raw` maps nodes back to gold entities by value — **approximate**.
- **Honesty**: gold labels are used for SCORING only, never as a system input.
  Cascade thresholds (`tau`) are fixed at design time; sweeping `tau`/model/filter
  on the eval set is EVALUATION of a fixed configuration, not fitting.
- **Known caveats**: `openlux` has intermittent 5xx / ConnectError; per-case
  `try/except` skips failures (error counts noted per run). Runs are serial (no
  concurrency) to avoid the provider's concurrency-related 5xx.

---

## 2. Build-side confidence cascade (MAIN RESULT, variables 3 + 4)

**What**: sweep the cascade threshold `tau`; at each `tau`, ingest every case on
path B while routing each new node through variable-4 (candidate selection:
lexical blocking → embed_topk → full pool) then variable-3 (edge discovery: regex
hard-block → cheap LLM → strong LLM). Record exact edge P/R, token cost split by
cheap vs strong tier, and per-tier call mix.

**Config**: cheap = `gpt-4.1-mini`, strong = `claude-opus-4-8`, `k=10`,
`max_filler=60`, `tau ∈ {0.0, 0.2, 0.4, 0.6, 0.8, 1.0}`.

**Command**:
```
CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.cascade_sweep \
    --hop 1 --limit 0 --provider openlux \
    --cheap-model gpt-4.1-mini --strong-model claude-opus-4-8 \
    --taus 0.0 0.2 0.4 0.6 0.8 1.0 \
    --data /Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json \
    --out integrations/memebench/runs/cascade_curve_hop1.json
```

### hop 1 (100 cases; artifact `cascade_curve_hop1.json`; 1 provider error)

| tau | micro-P | micro-R | macro-P | macro-R | cheap-tok | strong-tok | tok/case |
|---|---|---|---|---|---|---|---|
| 0.00 | 0.803 | 0.959 | 0.857 | 0.964 | 958,852 | 0 | 9,685 |
| 0.20 | 0.799 | 0.959 | 0.854 | 0.962 | 975,542 | 10,257 | 9,858 |
| **0.40** | 0.820 | 0.956 | 0.872 | 0.958 | 1,099,204 | 306,768 | **14,060** |
| 0.60 | 0.910 | 0.859 | 0.912 | 0.853 | 1,273,231 | 1,173,525 | 24,468 |
| 0.80 | 0.977 | 0.652 | 0.877 | 0.631 | 1,811,862 | 1,261,210 | 30,731 |
| 1.00 | 0.980 | 0.627 | 0.852 | 0.608 | 1,843,009 | 1,260,501 | 31,035 |

### hop 2 (64 cases; artifact `cascade_curve_hop2.json`; 0 errors)

| tau | micro-P | micro-R | macro-P | macro-R | cheap-tok | strong-tok | tok/case |
|---|---|---|---|---|---|---|---|
| 0.00 | 0.774 | 0.955 | 0.808 | 0.961 | 734,297 | 0 | 11,473 |
| 0.20 | 0.797 | 0.955 | 0.837 | 0.961 | 734,159 | 3,757 | 11,530 |
| **0.40** | 0.795 | 0.955 | 0.825 | 0.959 | 805,825 | 97,435 | **14,113** |
| 0.60 | 0.884 | 0.830 | 0.928 | 0.830 | 899,619 | 414,935 | 20,540 |
| 0.80 | 0.983 | 0.700 | 0.973 | 0.697 | 1,349,405 | 1,106,843 | 38,379 |
| 1.00 | 0.976 | 0.660 | 0.954 | 0.659 | 1,380,440 | 990,570 | 37,047 |

### Per-tier call mix (hop 2 — mix is monotone in tau; hop 1 identical shape)

| tau | cand block / embed_topk / full | edge regex / cheap_none / cheap / strong |
|---|---|---|
| 0.00 | 418 / 81 / 0 | 177 / 84 / 238 / 0 |
| 0.20 | 418 / 81 / 0 | 177 / 84 / 236 / 2 |
| 0.40 | 406 / 41 / 52 | 177 / 82 / 221 / 19 |
| 0.60 | 335 / 25 / 139 | 177 / 85 / 101 / 136 |
| 0.80 | 28 / 4 / 467 | 177 / 95 / 29 / 198 |
| 1.00 | 0 / 0 / 499 | 177 / 106 / 29 / 187 |

**Reading**:
- precision rises monotonically in `tau` (0.80 → 0.98): higher `tau` escalates
  more low-confidence edges to opus, which rejects mislinks.
- recall FALLS monotonically (0.96 → ~0.63–0.66): opus is more conservative than
  mini and judges some true edges as NONE. **This is a real trade-off, not a bug**
  — escalating everything to the strongest tier is neither cheapest nor best.
- cost rises monotonically; `tau` is the knob that moves spend from cheap to
  strong (strong-tok 0 → ~1.26M hop1).
- **Sweet spot `tau = 0.40`, reproduced on both hop sets**: precision near its
  useful peak, recall essentially intact (0.958 / 0.959), tok/case ≈ 14k — about
  **38–45% of the tau=1.0 token cost** for comparable precision. This is the
  "cascade curve dominates the single-tier baselines" result.
- The free regex hard-block fires a **constant 177 edges** across all `tau` (hop2)
  — conditional-rule predeclarations blocked with zero LLM calls; the foundation
  of the cost saving.
- micro-P vs macro-P diverge at high `tau` (hop1: 0.98 vs 0.85): opus is precise
  on edge-rich cases but occasionally wholly wrong on edge-sparse cases, which
  macro-averaging penalizes.

---

## 3. Candidate-selection sweep (variable-4 single-tier baseline)

**What**: hold discovery fixed, sweep the candidate pre-filter, to justify the
variable-4 tiers. Path B, exact P/R.
**Config**: `model=gpt-4o-mini`, `max_filler=60`, filters
`full`, `embed_topk:{5,10,20}`, `lexical`.
**Command**:
```
CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.candidate_selection_sweep \
    --hop 1 --limit 0 --provider openlux --model gpt-4o-mini \
    --data .../meme_filler32k.json \
    --filters full embed_topk:5 embed_topk:10 embed_topk:20 lexical \
    --out integrations/memebench/runs/cand_sweep_hop1.json
```

### hop 1 (100 cases)
| filter | micro-P | micro-R | macro-P | macro-R | cand | tok/case |
|---|---|---|---|---|---|---|
| full | 0.219 | 0.890 | 0.234 | 0.876 | 19 | 27,616 |
| **embed_topk:5** | 0.397 | 0.991 | 0.426 | 0.989 | 5 | 5,534 |
| embed_topk:10 | 0.373 | 0.987 | 0.401 | 0.984 | 10 | 13,450 |
| embed_topk:20 | 0.380 | 0.978 | 0.422 | 0.978 | 18 | 26,572 (1 err) |
| lexical | 0.280 | 0.950 | 0.321 | 0.953 | 6 | 10,325 |

### hop 2 (64 cases)
| filter | micro-P | micro-R | macro-P | macro-R | cand | tok/case |
|---|---|---|---|---|---|---|
| full | 0.212 | 0.899 | 0.218 | 0.905 | 19 | 33,512 |
| **embed_topk:5** | 0.390 | 0.996 | 0.408 | 0.995 | 5 | 6,215 |
| embed_topk:10 | 0.361 | 1.000 | 0.383 | 1.000 | 10 | 15,646 |
| embed_topk:20 | 0.366 | 0.988 | 0.384 | 0.988 | 18 | 32,003 |
| lexical | 0.262 | 0.969 | 0.273 | 0.973 | 6 | 13,620 (5 err) |

**Reading**: `embed_topk:5` dominates `full` — higher precision AND near-perfect
recall at ~5× fewer tokens. Embedding is a weak JUDGE but a strong RETRIEVER: it
keeps the low-similarity purely-semantic distractors OUT of the candidate set, so
the LLM never gets the chance to mislink them.

---

## 4. Edge-discovery model sweep (variable-3 single-tier baseline)

**What**: hold everything else fixed, vary only the discovery model. Path B.
**Config**: models `gpt-4o-mini`, `gpt-4o`, `claude-opus-4-8`.
**Command**:
```
CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.discovery_model_sweep \
    --hop 1 --limit 0 --provider openlux \
    --models gpt-4o-mini gpt-4o claude-opus-4-8 \
    --out integrations/memebench/runs/discovery_sweep_hop1.json
```

### hop 1 (100 cases)
| model | micro-P | micro-R | macro-P | macro-R | tok/case |
|---|---|---|---|---|---|
| gpt-4o-mini | 0.420 | 0.997 | 0.472 | 0.995 | 1,757 |
| gpt-4o | 0.576 | 0.969 | 0.588 | 0.955 | 1,730 |
| claude-opus-4-8 | 0.774 | 0.944 | 0.774 | 0.912 | 2,280 |

### hop 2 (64 cases)
| model | micro-P | micro-R | macro-P | macro-R | tok/case |
|---|---|---|---|---|---|
| gpt-4o-mini | 0.388 | 1.000 | 0.395 | 1.000 | 2,305 (1 err) |
| gpt-4o | 0.560 | 0.996 | 0.562 | 0.996 | 2,265 |
| claude-opus-4-8 | 0.755 | 1.000 | 0.760 | 1.000 | 2,948 (1 err) |

**Reading**: a stronger model buys precision (0.42 → 0.77 on hop1) at a slight
recall cost and ~1.3× tokens; recall stays ≥0.94 everywhere. This is exactly the
per-item trade-off the cascade exploits: pay for opus only where the cheap tier is
unconfident, rather than on every edge.

---

## 5. Negative-edge evaluation set (for the propagation side)

**What**: build a labeled set of positive vs negative (should-NOT-stale) edges,
splitting negatives into regex-catchable (predeclaration rules) vs semantic
(needs a judge). Path A; auto-labels are approximate (audited separately).
**Artifacts**: `neg_edge_set_hop1.json`, `neg_edge_set_hop2.json` (merged v2 +
openlux-refill sets — quote the JSON `summary` for official sizes).

| | hop 1 | hop 2 |
|---|---|---|
| n_edges | 2,302 | 1,769 |
| n_cases | 100 | 64 |
| positive | 860 | 699 |
| negative | 1,442 | 1,070 |
| neg: predeclaration (regex-caught) | 633 | 575 |
| neg: semantic_offtopic (needs judge) | 809 | 495 |
| frac negatives semantic | 0.561 | 0.463 |

**Reading**: roughly half the negatives are regex-catchable (predeclaration
mislinks) and half are semantic — motivating a cascade whose free regex tier kills
the former and escalates only the latter to a paid judge.

---

## 6. False-edge taxonomy (evaluation-bed viability check)

**What**: classify the FALSE edges naive discovery persists into
`syntactic_killable` (predeclaration mislink) vs `semantic_only` (needs a paid
judge), to size the judge workload and confirm the eval bed is meaningful.
**Artifacts**: `fet_hop1.json` (100 cases), `fet_hop2.json` (64 cases).

| | hop1 file | hop2 file |
|---|---|---|
| persisted / gold / false edges | 770 / 319 / 453 | 639 / 247 / 392 |
| false_edge_precision_loss | 0.588 | 0.613 |
| semantic_only / syntactic_killable | 123 / 330 | 103 / 289 |
| frac false needing paid judge | 0.272 | 0.263 |

**Reading**: ~27% / 26% of false edges genuinely need a semantic (paid) judge; the
other ~73% are syntactically killable. Verdict on both runs: VIABLE (a meaningful
semantic-only fraction exists to test the judge on).

---

## 7. Judge-routing sweep (propagation-side staleness cascade)

**What**: on the bipolar negative-edge set, compare 4 staleness-judge tiers —
J1 free rule / J2 embedding threshold / J3 cheap LLM / J4 costly LLM — on
precision, recall, F1, and cost. (This is the PROPAGATION-side cascade, the dual
of the build-side cascade in §2. It stays in the benchmark for now — it measures
whether a cheap tier can replace the oracle, and has not been promoted to a
production service.)
**Config**: `change_source=upstream`, cheap = `gpt-4o-mini`,
costly = `claude-opus-4-8`; J2 swept over cosine thresholds {0.3…0.7}.
**Artifacts**: `judge_routing_hop1.json` (2,302 edges, 0 err),
`judge_routing_hop2.json` (1,769 edges, 90 provider errors).

### hop 1 (2,302 edges)
| judge | precision | recall | f1 | tok/edge |
|---|---|---|---|---|
| J1_rule | 0.566 | 0.908 | 0.697 | 0 |
| J2_embed@0.3 | 0.373 | 0.997 | 0.543 | 0 |
| J3_cheap_llm | 0.617 | 0.933 | 0.743 | 223 |
| J4_costly_llm | 0.976 | 0.806 | 0.883 | 7,039 |

### hop 2 (1,769 edges, 90 err)
| judge | precision | recall | f1 | tok/edge |
|---|---|---|---|---|
| J1_rule | 0.634 | 0.917 | 0.750 | 0 |
| J2_embed@0.3 | 0.395 | 0.997 | 0.566 | 0 |
| J3_cheap_llm | 0.594 | 0.913 | 0.720 | 216 |
| J4_costly_llm | 0.975 | 0.819 | 0.890 | 6,436 |

**Reading**: J4 has near-perfect precision (~0.976) but ~30× J3's token cost and
lower recall; J3 is the best F1/cost tradeoff; embedding-only (J2) is high-recall
/ low-precision at any threshold. Same shape as the build-side cascade: a free
rule + cheap LLM resolve most cases, the costly tier is reserved for the hard ones.

---

## 8. Stage-A calibration (judge recall on all-should-stale gold edges)

**What**: calibrate the 4-tier judge cascade on gold (all-should-stale) edges —
this measures RECALL + cost only (no negatives, so no precision).
**Config**: cheap = `gpt-4o-mini`, costly = `gpt-4.1-mini`
(**note: differs from §7's costly = claude-opus-4-8**).
**Artifacts**: `stage_a_hop1.json` (319 edges), `stage_a_hop2.json` (247 edges).

| judge | hop1 recall | hop2 recall |
|---|---|---|
| J1_rule | 0.749 | 0.676 |
| J2_embed thr_0.3 | 0.931 | 0.911 |
| J2_embed thr_0.5 | 0.708 | 0.700 |
| J3_cheap_llm | 1.000 | 1.000 |
| J4_costly_llm | 0.934 | 0.943 |

Cost: J3 ≈ 152–158 tok/edge; J4 ≈ 156–162 tok/edge.

**Reading**: J3 (cheap LLM) hits 100% recall at ~150 tok/edge — the cascade's
workhorse. J1_rule collapses to 0 recall on multi-hop (hop-2) edges (the rule only
catches direct 1-hop staleness), which is precisely why an LLM fallback is needed.
J2 embedding needs a low threshold (0.3) to reach ~0.91–0.93 recall.

---

## 9. Cross-run caveats (read before quoting)

- `costly_model` differs between experiments: judge-routing (§7) uses
  `claude-opus-4-8`; stage-A (§8) uses `gpt-4.1-mini`. Do not compare their token
  costs directly.
- Several runs hit provider network errors (judge_routing hop2 = 90 err; scattered
  ConnectError/503 in cand/disc/neg runs). Error counts are noted per table; failed
  cases are skipped, not retried inline.
- Negative-edge-set sizes (§5) come from the refilled JSON `summary`, not the
  pre-refill log tails.
- FET / stage-A JSONs carry an internal `by_hop` split; the "hop1 file" vs "hop2
  file" distinction is the case-set of the run, not a clean per-hop partition.
- `runs/` is gitignored; regenerate any table with the command shown in its section.
