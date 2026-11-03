# Deferred: automatic recall and memory capture evaluation

Status: independent research question; outside the explicit-tool adapter scope.
Recorded on 2026-11-03.

The OpenClaw integration searches before a turn and can save selected assistant
answer fragments after a turn. Its capture heuristic does not establish whether
those fragments are accurate, useful, or suitable as lasting memories. Porting
the hooks to Deep Agents would not resolve that uncertainty.

Before deciding whether to add automatic hooks, define an annotated set of
multi-turn conversations and measure:

- Capture: correct facts, unsupported or incorrect claims, duplicates, and facts
  that should have been saved but were missed.
- Recall: relevant memories retrieved, important memories missed, stale facts,
  and effects on answer correctness and source citations.
- End-to-end tradeoffs: answer quality, token use, and latency compared with
  explicit tools and a no-memory baseline, including conflicting updates and
  account/agent access boundaries.

Datasets, labels, and acceptance thresholds remain undecided. HyDRA-Bench or
FDABench SQL/text retrieval experiments are a separate workstream; passing them
would not by itself validate automatic conversational memory.

The current adapter and its live checks cover explicit ContextHub tool calls.
No automatic recall/capture behavior is being added as part of this work.
