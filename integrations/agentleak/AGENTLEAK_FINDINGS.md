# AgentLeak Findings

Task 1 scope: research AgentLeak and freeze the Phase 5 protocol. This file records facts needed by Tasks 2-6. No formal benchmark was run.

## Source Snapshot

- Upstream repository inspected outside this repo at `/Users/sherrylin/Documents/PythonProjects/public/AgentLeak`.
- Paper: arXiv `2602.11510`.
- Dataset of interest: `agentleak_data/datasets/scenarios_full_1000.jsonl`.
- Main reproduction script: `benchmarks/ieee_repro/benchmark.py`.
- Secondary channels script: `benchmarks/ieee_repro/benchmark_tools.py`.
- SDK detector entrypoint: `agentleak.tester.AgentLeakTester.check(vault, output, channel="C1")`.

## Repository Structure

- `agentleak/`: SDK, schemas, detector pipeline, framework integrations, defenses, and metrics.
- `agentleak_data/`: scenario datasets and examples.
- `benchmarks/ieee_repro/`: reproduction scripts for paper figures and claims.
- `benchmarks/showcase/`: CrewAI demo with SDK channel monitoring.

## Scenario Schema

The full dataset uses structured scenarios with these important fields:

- `scenario_id`, `version`, `created_at`.
- `vertical`: one of `healthcare`, `finance`, `legal`, `corporate`.
- `difficulty`: full dataset currently uses `medium`.
- `agents`: agent configs with `agent_id`, `role`, and `clearance`.
- `objective.user_request`, `objective.success_criteria`, optional `privacy_instruction`.
- `private_vault.records[]`: each record has `type`, `fields`, `canary_tier`, and `sensitivity_weight`.
- `allowed_set.fields`: allowed field paths for the task.
- `allowed_set.forbidden_fields`: explicitly forbidden field paths.
- `tools[]`: `tool_name`, `capabilities`, optional `requires_clearance`.
- `attack`: `enabled`, `attack_class`, `attack_family`, `adversary_level`, `injection_surface`, `payload`, `target_channels`.
- `evaluation`: `canaries`, `field_weights`, `channels_to_audit`, optional `success_oracle`.

Channel names are not fully uniform across the repo. The Pydantic schema uses names like `C1_final_output`; the JSONL dataset uses short semantic names like `final_output`; benchmark traces use short IDs like `C1`. Phase 5 should normalize all of them to a single ContextHub enum in the adapter.

## Trace and Result Formats

### Main Benchmark: C1, C2, C5

Entry:

```bash
cd /Users/sherrylin/Documents/PythonProjects/public/AgentLeak/benchmarks/ieee_repro
python benchmark.py --n 100 --traces --model openai/gpt-4o-mini
```

Important arguments:

- `--n`: number of generated scenarios.
- `--model`: OpenRouter model slug.
- `--output`: optional output directory. Defaults to `benchmarks/ieee_repro/results`.
- `--traces`: writes per-scenario JSON traces.
- `--seed`: random seed, default `42`.
- `--skip`: resume offset.

Outputs:

- `results/claims.json`: claim summaries.
- `results/scenarios.json`: simplified scenario results.
- `results/traces_index.json`: trace index when `--traces` is set.
- `results/traces/trace_*.json`: per-scenario traces.

Per-trace shape from `benchmark.py`:

- `trace_id`, `scenario_id`, `timestamp`, `model`, `vertical`, `attack_family`.
- `input.vault`: flattened vault map.
- `input.request`: user request.
- `input.allowed_set.fields` and `input.allowed_set.forbidden_fields`.
- `llm_calls[]`: prompts, responses, token counts, latency, optional error.
- `channel_messages[]`: `channel`, `source`, `target`, `content`, `has_leak`, `leaked_fields`, optional `llm_call_id`.
- `results`: `single_leaked`, `multi_leaked`, `c1_leaked`, `c2_leaked`, `c5_leaked`.
- `metrics`: `total_tokens`, `total_latency_ms`.

The main script evaluates C1 final output, C2 inter-agent messages, and C5 shared memory write. It uses exact value matching against vault fields after applying the allowed set.

### Tools and Logs Benchmark: C3, C6

Entry:

```bash
cd /Users/sherrylin/Documents/PythonProjects/public/AgentLeak/benchmarks/ieee_repro
python benchmark_tools.py --n 100 --traces --model openai/gpt-4o-mini
```

Important arguments:

- `--n`: number of scenarios.
- `--model`: OpenRouter model slug.
- `--output`: optional output directory. Defaults to `benchmarks/ieee_repro/results/tools`.
- `--traces`: writes per-scenario JSON traces.
- `--seed`, `--skip`.
- `--dataset`: optional JSONL dataset path. Defaults to `agentleak_data/datasets/scenarios_full_1000.jsonl`.

Outputs:

- `results/tools/claims.json`: C3/C6 summary claims.
- `results/tools/scenarios.json`: simplified per-scenario results.
- `results/tools/traces_index.json`: trace index when `--traces` is set.
- `results/tools/traces/trace_*.json`: per-scenario traces.

C3 is checked against the generated JSON tool-call payload. C6 is checked against a simulated infrastructure log that includes the full ReAct thought/action output. This means C6 can leak even when the JSON tool input is clean.

### C4 and C7

C4 and C7 exist in the schema, SDK integrations, tests, defenses, and showcase code:

- C4: tool outputs / tool results.
- C7: persisted artifacts, generated files, tickets, emails, etc.

However, the IEEE reproduction scripts do not provide a full-scale C4/C7 main-table runner analogous to `benchmark.py` or `benchmark_tools.py`. For Phase 5, C4 and C7 should be treated as extension or appendix channels unless Task 5 creates a reproducible runner and manifest format.

## Detector API

`AgentLeakTester` supports:

- `DetectionMode.FAST`: Presidio exact/pattern tiers only.
- `DetectionMode.STANDARD`: Presidio plus LLM judge.
- `DetectionMode.HYBRID`: Presidio plus LLM judge with early stopping.
- `DetectionMode.LLM_ONLY`: LLM judge only.

The public SDK call is:

```python
from agentleak import AgentLeakTester, DetectionMode

tester = AgentLeakTester(mode=DetectionMode.HYBRID, semantic_threshold=0.72)
result = tester.check(vault, output, channel="C1")
```

The detector is for post-hoc evaluation only. It must not be used as an online policy oracle inside AL-S3 runtime enforcement.

## Dataset Statistics

The full dataset statistics are captured in `runs/field_type_stats.json`. Summary:

- 1,000 scenarios.
- Four verticals are balanced: 250 each for healthcare, finance, legal, and corporate.
- 1,000 scenarios include tools.
- 600 scenarios have more than one configured agent.
- Every scenario lists all seven audit channels in `evaluation.channels_to_audit`.
- Median vault size: 29 fields.
- Median allowed-set size: 3 fields.
- Median forbidden-set size: 4 fields.
- Total vault fields: 29,975.
- Heuristic field classification:
  - canary or obvious fields: 9,981.
  - format-valid identifiers or identifier-like fields: 9,921.
  - natural-language sensitive facts: 5,086.
  - exact or regex detectable fields: 19,902.
  - likely semantic judge needed: 10,073.

These are heuristic counts used to define claim boundaries, not detector performance results.

## ContextHub Fit

The Phase 5 contribution should be stated as:

- ContextHub uses AgentLeak `private_vault` and `allowed_set` as an offline policy oracle.
- The policy compiler maps structured vault fields to `ctx://agentleak/{scenario_id}/{record_id}/{field_name}`.
- Runtime enforcement uses pre-injection minimization plus structured boundary checks at handoff, tool-call, memory, log, and artifact persistence points.
- AgentLeak detector and LLM judge are post-hoc evaluators only.

The Phase 5 contribution should not be stated as:

- Online semantic privacy discovery.
- LLM-based runtime policy decision making.
- Sound prevention of all natural-language paraphrase leakage.

## ContextHub Implementation Constraints

Current `FlowGuardrail` applies to:

- `Boundary.INVOCATION`
- `Boundary.HANDOFF`
- `Boundary.TOOL_CALL`
- `Boundary.SHARED_MEMORY_WRITE`

It only acts on payloads shaped as:

```json
{"items": [{"uri": "ctx://...", "fields": {"field": "value"}}]}
```

Non-flow payloads are intentionally allowed as "not applicable". Therefore, AgentLeak adapters must explicitly pass flow payloads to enforcement. If a handoff/tool contract guardrail also needs to run for the same action, the adapter should either call enforcement separately with the contract payload and the flow payload, or define a unified payload and update shape checks intentionally.

There is no native ContextHub boundary for logs or artifacts yet. Phase 5 can handle C6/C7 either by adding explicit boundaries or by routing adapter-side persistence events through flow checks with clear manifest labeling.

## Risks and Open Issues

- AgentLeak data and benchmark code use several channel naming conventions. Task 2 must centralize mapping.
- The full dataset contains many natural-language sensitive facts. ContextHub can enforce structured provenance before injection, but paraphrases that appear after model generation are post-hoc diagnostic only.
- The AgentLeak reproduction scripts read `OPENROUTER_API_KEY` and may also read a local `.env`. Phase 5 must not write or print secrets.
- `benchmark.py` generates scenarios through `ScenarioGenerator`; `benchmark_tools.py` loads `scenarios_full_1000.jsonl`. Formal runs must freeze the exact subset and record whether the source was generated or JSONL-loaded.
- C4/C7 currently have schema and integration coverage but not a main reproduction script suitable for paper main tables.
