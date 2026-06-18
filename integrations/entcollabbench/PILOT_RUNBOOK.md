# EntCollabBench Pilot Runbook

This runbook records the local steps needed before running EntCollabBench pilots
with ContextHub Phase 4 runtime enforcement. Do not put real API keys in this
file. Keep secrets in `secrets/entcollab_models.env`, which is git-ignored.

## 1. Prerequisites

From the ContextHub repo:

```bash
cd /Users/sherrylin/Documents/PythonProjects/ContextHub
```

Make sure local model secrets exist:

```bash
test -f secrets/entcollab_models.env
source scripts/entcollab_env.sh weak
# or:
source scripts/entcollab_env.sh strong
# or pass a custom model id:
source scripts/entcollab_env.sh gpt-4o-mini
```

The loader exports:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `AGENT_LLM_MODEL`
- `AGENT_SUMMARY_MODEL`
- `JUDGE_OPENAI_API_KEY`
- `JUDGE_OPENAI_BASE_URL`
- `JUDGE_MODELS`
- `NO_PROXY` / `no_proxy`

For the current local setup, `OPENAI_BASE_URL` and `JUDGE_OPENAI_BASE_URL`
must include the OpenAI-compatible API path, e.g. `https://yunwu.ai/v1`.

## 2. Start Docker Desktop

```bash
open /Applications/Docker.app
docker info
docker compose version
```

`docker info` must connect to the Docker Desktop daemon. If it fails with a
socket error, wait for Docker Desktop to finish starting.

## 3. Start EntCollabBench MCP Services

```bash
cd /Users/sherrylin/Documents/PythonProjects/research/EntCollabBench
docker compose -f Arena/docker-compose-mcp.yml up -d --force-recreate
docker compose -f Arena/docker-compose-mcp.yml ps
```

Expected services:

- `mcp-calendar`
- `mcp-csm`
- `mcp-drive`
- `mcp-email`
- `mcp-hr`
- `mcp-itsm`
- `mcp-teams`

All should be `healthy`.

When probing MCP endpoints from Python, use:

```bash
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost"
export no_proxy="${no_proxy:+$no_proxy,}127.0.0.1,localhost"
```

Without this, localhost probes may return proxy `502` errors.

## 4. Start EntCollabBench Agent Services

Load a profile first:

```bash
cd /Users/sherrylin/Documents/PythonProjects/ContextHub
source scripts/entcollab_env.sh strong
```

Then start agents:

```bash
cd /Users/sherrylin/Documents/PythonProjects/research/EntCollabBench
docker compose -f agent/docker-compose.yml build
docker compose -f agent/docker-compose.yml up -d --force-recreate
```

Check health:

```bash
NO_PROXY=127.0.0.1,localhost python - <<'PY'
from urllib.request import urlopen
agents = {
    "it_service_desk_l1": 18001,
    "it_change_engineer": 18002,
    "hr_service_specialist": 18003,
    "customer_support_specialist": 18004,
    "knowledge_base_specialist": 18005,
    "collaboration_ops_specialist": 18006,
    "developer_engineer": 18007,
    "qa_test_engineer": 18008,
    "finance_approval_specialist": 18009,
    "legal_approval_specialist": 18010,
    "procurement_approval_specialist": 18011,
}
for name, port in agents.items():
    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
        print(name, resp.status)
PY
```

## 5. Recommended Smoke Order

Do not start with random `--sample-k 1`; it may select a complex task and burn
many tokens. Use this order instead:

1. Zero-tool agent smoke:
   - direct POST to `collaboration_ops_specialist`
   - task: `Reply exactly: DONE: smoke test complete. UNDONE: none. ERROR: none.`
2. Single-tool read smoke:
   - seed a CSM DB
   - ask `customer_support_specialist` to read `CS-0000050`
3. Single-tool write smoke:
   - seed a Teams DB
   - ask `collaboration_ops_specialist` to send one message
   - export Teams state and verify `channel_messages`
4. ContextHub S2 gate smoke:
   - use `WorldLoader`
   - call `EnforcementInterceptor.on_tool_call()`
   - only execute the agent action if verdict is `allow`
   - verify `audit_log.action = 'enforcement'` and `metadata.verdict`

Known validated local results:

- zero-tool agent smoke passed
- CSM read smoke passed
- Teams write smoke passed
- S2 gated Teams write smoke passed

## 6. Benchmark Pilot Strategy

Current finding: full EntCollabBench tasks are much heavier than the small
runtime smoke tasks. Even a fixed task like `mcp_single_144` has 8 expected
steps and 3 delegations, and timed out under both weak and strong profiles.

Therefore:

- Use deterministic fixed task specs, not random `--sample-k`, for early pilots.
- Keep `--batch-concurrency 1`.
- For comparable pilot runs, use the EntCollabBench paper/repo timeout profile:
  `TASK_TIMEOUT_SECONDS=1000`, `AGENT_HTTP_TIMEOUT_SECONDS=400`, and
  `JUDGE_TIMEOUT_SECONDS=500`.
- Shorter timeouts are only for smoke/debug runs and should not be mixed into
  formal failure-distribution analysis.
- Start with one subset and one model profile.
- Treat timeout as runtime/agent behavior evidence, not an environment failure,
  if MCP, agent health, model calls, and tool calls are visible in logs.

First fixed-case boundary:

- Use `experiments/fixed12/FIXED_CASES_MANIFEST_12.json` for the first frozen
  12-case suite.
- Use `experiments/fixed12/FIXED_CASES_RUNNER_PROMPT.md` when delegating the run
  to another agent.
- Use `experiments/fixed12/FIXED_CASES_REPORT_TEMPLATE.md` for the final
  fixed-suite report.
- The intended execution is fresh S0 benchmark plus near-online/post-run S2
  diagnostic over saved artifacts, not full online S2.
- Do not randomize, expand, or reorder the fixed cases without explicitly
  updating the manifest and report boundary.
- Write all generated reports and raw benchmark artifacts under a timestamped
  `integrations/entcollabbench/runs/` directory, not the package root.

Example fixed-task run:

```bash
cd /Users/sherrylin/Documents/PythonProjects/ContextHub
source scripts/entcollab_env.sh strong

cd /Users/sherrylin/Documents/PythonProjects/research/EntCollabBench
MCP_ENDPOINTS_FILE=/Users/sherrylin/Documents/PythonProjects/research/EntCollabBench/config/mcp_endpoints_export.json \
TASK_TIMEOUT_SECONDS=1000 \
AGENT_HTTP_TIMEOUT_SECONDS=400 \
JUDGE_TIMEOUT_SECONDS=500 \
.venv/bin/python scripts/benchmark.py \
  --tasks-spec-file scripts/result/contexthub_smoke_mcp_single_144.json \
  --batch-concurrency 1 \
  --agent-url-map-file config/agent_url_map.json \
  --trajectory-full-mode \
  --bench-result-jsonl scripts/result/contexthub_smoke_strong_result.jsonl \
  --trajectory-run-jsonl scripts/result/contexthub_smoke_strong_traj.jsonl \
  --continue-on-error
```

## 7. System Conditions: S0, S1, S2

Phase 4 uses system conditions to separate "model capability" from "runtime
context governance." Run the same model and same task set under different
system conditions.

### S0: No Guardrail Baseline

S0 is the original EntCollabBench runtime without ContextHub-backed runtime
enforcement.

Use S0 to answer: what can the model and EntCollabBench agent loop do by
themselves?

In S0:

- agents delegate normally
- agents call MCP tools normally
- agents announce final completion normally
- benchmark judge / state diff scores the result afterwards
- ContextHub does not block, repair, or audit runtime boundaries

S0 is required as the baseline for task success, timeout, token usage, and
failure-mode discovery.

### S1: Generic Guardrail Baseline

S1 is a generic guardrail condition that does not read ContextHub state. It is a
control for "does any guardrail help?"

S1 may check only static/generic properties, such as:

- tool name is in a static allowlist
- required args are present
- simple enum/type constraints are satisfied
- final output is non-empty
- approval label is in a static allowed-label set

S1 must not read:

- ContextHub `contexts`
- ACL / ownership / field masks
- staleness / version state
- dependencies / provenance
- loaded world object existence
- closure evidence from ContextHub

If S2 beats S1, the gain is more likely due to ContextHub-backed
ownership/version/provenance/closure context rather than a generic guardrail.

### S2: ContextHub-Backed Runtime Enforcement

S2 is the Phase 4 method. It uses ContextHub state plus runtime contracts to
make `allow / block / repair / escalate` decisions at execution boundaries.

S2 can use:

- ACL / ownership / field masks
- context version and staleness (`ctx://...@vN` runtime refs)
- handoff packets
- live MCP tool schemas normalized into `ToolCallContract`
- closure checklists generated from ground truth, trace, state diff, and
  timeout/partial trace boundaries
- object existence / provenance adapters
- audit logging with `action="enforcement"`, `result="success"`, and the real
  verdict in `metadata["verdict"]`

S2 can reasonably claim improvements for:

- incomplete handoff packets
- unauthorized context flow
- schema/enum/required-arg errors
- wrong role / wrong object mutation
- stale or version-mismatched dependencies
- missing closure actions/evidence
- weak approval decisions missing rule citations

S2 should not claim raw provider latency, generic model planning loops, or
judge-only mismatch unless the adapter converts the partial trace into an
explicit unmet workflow obligation.

### Experiment Goal

The intended comparison is:

```text
S0: no guardrail, score after the fact
S1: generic guardrail, no ContextHub state
S2: ContextHub-backed runtime enforcement
```

The core hypotheses are:

- `S2 > S0`: ContextHub-backed enforcement improves downstream task/closure
  success or reduces unsafe/wrong actions.
- `S2 > S1`: the gain is not just from adding any generic guardrail.
- The `S2 - S0` gain should not disappear under stronger models; otherwise the
  method may be acting like prompt engineering rather than context governance.

Current implementation status:

- S0 baseline runner is available via EntCollabBench `scripts/benchmark.py`.
- S1 is implemented as a mock/generic system condition for unit-testable
  evaluation scaffolding, but has not yet been a main pilot focus.
- S2 has working core guardrails, adapter helpers, near-online diagnostics, and
  a ContextHub-owned `runtime_wrapper.py`.
- Tool-call S2 can be used as a true pre-dispatch gate in a harness.
- Closure S2 currently runs at result/timeout/partial-trace boundaries.
- Handoff S2 has a thin packet-based wrapper but is not yet fully inserted into
  EntCollabBench's internal delegation path.

## 8. Prompt For Another Agent

Use this prompt when delegating pilot execution to another agent:

```text
You are running ContextHub Phase 4 EntCollabBench pilot smoke tests.

Repository:
- ContextHub: /Users/sherrylin/Documents/PythonProjects/ContextHub
- EntCollabBench external clone: /Users/sherrylin/Documents/PythonProjects/research/EntCollabBench

Read first:
- /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/PILOT_RUNBOOK.md
- /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/ENTCOLLABBENCH_FINDINGS.md
- /Users/sherrylin/Documents/PythonProjects/ContextHub/scripts/entcollab_env.sh

Rules:
- Do not print or expose API keys.
- Do not commit changes.
- Do not run random large benchmark batches.
- Always use NO_PROXY/no_proxy for localhost probes.
- Prefer fixed tiny smokes before full benchmark tasks.
- Stop and report if token usage appears runaway or a task loops.

Goal:
1. Verify Docker Desktop, MCP services, and agent services are healthy.
2. Load model env with `source scripts/entcollab_env.sh strong`.
3. Run zero-tool, single-tool read, single-tool write, and ContextHub S2 gate smokes.
4. If all pass, run one fixed benchmark task with short timeout and batch-concurrency 1.
5. Return result paths, task status, token usage, trace event counts, and any failure reason.

Known caveats:
- `OPENAI_BASE_URL` must include `/v1` for the current provider.
- MCP `/api/export-state` may not exist for all services; fallback to `/api/database-state` with `x-database-id`.
- Full EntCollabBench tasks may timeout even when the environment is healthy.
```
