# AgentLeak Secondary Channels

Task 5 adds adapter coverage for `benchmark_tools.py`, which is the public IEEE
reproduction entry for secondary channels:

- C3 tool input / tool arguments.
- C6 infrastructure logs.

The adapter reuses the Task 2 normalized trace schema. C3 events use
`channel="C3"` and `event_type="tool_call"`; C6 events use `channel="C6"` and
`event_type="log_event"`. Both emit Task 2 `flow_items` as value-free
`ctx://agentleak/...` references and keep raw payloads behind `content_ref`.

## Runtime Claim Boundary

The C3/C6 adapter only maps structured payloads and exact vault-value matches to
flow items. It does not call AgentLeak detectors, LLM judges, or any online
policy oracle. Semantic or paraphrased free-text leakage is marked as diagnostic
and must be evaluated post-hoc by Task 6.

For C3, metadata preserves:

- `tool_name`.
- `tool_arguments`.
- `sensitive_fields_in_arguments`.
- `logical_boundary="tool_call"`.

For C6, metadata preserves:

- `log_source`.
- `log_level`.
- `structured_fields`.
- `structured_sensitive_fields`.
- `logical_boundary="log_persistence"`.

`log_persistence` is a logical Phase 5 boundary label, not a fake
`Boundary.HANDOFF` or `Boundary.TOOL_CALL` mapping.

## C4 / C7 Reproducibility

The public IEEE reproduction scripts currently provide:

- `benchmark.py` for C1/C2/C5.
- `benchmark_tools.py` for C3/C6.

The showcase application demonstrates SDK monitoring and includes C4-like and
C7-adjacent persistence concepts, but it is not a scenario-subset runner aligned
with the 1,000-scenario benchmark, fixed subset manifests, and normalized trace
protocol. Therefore C4 and C7 should remain appendix/future-work channels unless
a manifest-grade public runner is added before formal runs.

C7 must not be filled with synthetic artifact data for the Phase 5 main table.
Task 6 should record C7 as excluded with reason
`no_public_ieee_repro_runner_for_c7_artifacts` unless a reproducible runner is
introduced and frozen.
