# ContextHub x EntCollabBench Fixed-12 Pilot Report

This report covers the first fixed-case pilot:

- S0: fresh EntCollabBench benchmark with no ContextHub runtime enforcement.
- S2: near-online/post-run diagnostic over the saved S0 results and trajectories.
- This is not a full online S2 run.

## Run Metadata

- Run directory:
- Manifest:
- S0 result files:
- S0 trajectory files:
- S2 summary file:
- Model profile:
- Timeouts: `TASK_TIMEOUT_SECONDS=1000`, `AGENT_HTTP_TIMEOUT_SECONDS=400`, `JUDGE_TIMEOUT_SECONDS=500`
- Batch concurrency: `1`
- Random sampling: no
- Cases expanded or reordered: no
- Stop condition triggered: yes/no
- Stop reason:

## Case Table

| Case | Stratum | S0 status | Task passed | Failed agents | High cost | Primary baseline category | S2 focus | Result / trajectory |
|---|---|---|---:|---|---:|---|---|---|
| `mcp_single_146` | prior_5_csm_itsm_teams |  |  |  | false | pass/control | false-block control |  |
| `mcp_single_115` | Gitea/ITSM/Teams |  |  |  | false | pass/control | Gitea tool_state control |  |
| `mcp_single_72` | HR/ITSM/Teams-like |  |  |  | true | token-heavy pass control | false-block control |  |
| `mcp_single_137` | HR/ITSM/Teams-like |  |  |  | true | token-heavy pass control | false-block control |  |
| `mcp_single_145` | prior_5_csm_itsm_teams |  |  |  | true | timeout / closure | closure, timeout boundary |  |
| `mcp_single_143` | prior_5_csm_itsm_teams |  |  |  | false | timeout / closure | closure, timeout boundary |  |
| `mcp_single_52` | CSM/ITSM expansion |  |  |  | true | closure / loop | closure, tool_state |  |
| `mcp_single_61` | CSM/ITSM expansion |  |  |  | true | closure / tool args / loop | closure, tool_state |  |
| `mcp_single_64` | CSM/ITSM expansion |  |  |  | false | closure / tool args | closure, tool_state |  |
| `mcp_single_67` | CSM/ITSM expansion |  |  |  | false | tool args/object | tool_state |  |
| `mcp_single_151` | Drive/Gitea/ITSM |  |  |  | false | tool args/object | Drive/Gitea/ITSM tool_state |  |
| `mcp_single_87` | HR/ITSM/Calendar/Email |  |  |  | false | tool args/object | HR/calendar/email tool_state |  |

## S0 Distribution

| Metric | Count | Cases |
|---|---:|---|
| Passed |  |  |
| Failed |  |  |
| Timeout |  |  |
| Stopped |  |  |
| Cleanup failures |  |  |
| Memory clear failures |  |  |
| Export-state compatibility errors |  |  |

Notes:

- Compare this distribution to the prior 20-case baseline only as context; the
  claim unit for this report is the fixed-12 suite.
- Passing but token-heavy cases are efficiency controls, not S2 correctness wins.

## Tokens And Duration

| Case | Run tokens | Judge tokens | Total tokens | Duration seconds | Runner elapsed seconds | High cost note |
|---|---:|---:|---:|---:|---:|---|
| `mcp_single_146` |  |  |  |  |  |  |
| `mcp_single_115` |  |  |  |  |  |  |
| `mcp_single_72` |  |  |  |  |  |  |
| `mcp_single_137` |  |  |  |  |  |  |
| `mcp_single_145` |  |  |  |  |  |  |
| `mcp_single_143` |  |  |  |  |  |  |
| `mcp_single_52` |  |  |  |  |  |  |
| `mcp_single_61` |  |  |  |  |  |  |
| `mcp_single_64` |  |  |  |  |  |  |
| `mcp_single_67` |  |  |  |  |  |  |
| `mcp_single_151` |  |  |  |  |  |  |
| `mcp_single_87` |  |  |  |  |  |  |

Summary:

- Min / median / mean / max run tokens:
- Min / median / mean / max duration:
- Token anomalies:
- Cases stopped by token stop condition:

## Failure Taxonomy

| Category | Count | Cases | S2 claim boundary |
|---|---:|---|---|
| Pass/control |  |  | False-block control only |
| Token-heavy pass |  |  | Efficiency warning only |
| Missing closure action/evidence |  |  | Claim only when closure checklist identifies unmet obligations |
| Wrong/missing tool args |  |  | Claim only when live-schema-normalized tool_state flags concrete violations |
| Object-state/provenance mismatch |  |  | Claim only with observed object/state evidence |
| Provider latency / timeout |  |  | Not a raw ContextHub claim |
| Incomplete / weak handoff |  |  | Partial unless structured handoff packets are intercepted |
| Repeated tool loop / planning loop |  |  | Claim only if converted into explicit closure/tool_state violations |
| Cleanup/memory failure |  |  | Environment/runtime note unless it affects closure evidence |

## S2 Closure Summary

| Case | Closure verdict | Missing actions | Open questions | Boundary type | Notes |
|---|---|---|---|---|---|
| `mcp_single_146` |  |  |  |  |  |
| `mcp_single_115` |  |  |  |  |  |
| `mcp_single_72` |  |  |  |  |  |
| `mcp_single_137` |  |  |  |  |  |
| `mcp_single_145` |  |  |  |  |  |
| `mcp_single_143` |  |  |  |  |  |
| `mcp_single_52` |  |  |  |  |  |
| `mcp_single_61` |  |  |  |  |  |
| `mcp_single_64` |  |  |  |  |  |
| `mcp_single_67` |  |  |  |  |  |
| `mcp_single_151` |  |  |  |  |  |
| `mcp_single_87` |  |  |  |  |  |

## S2 Tool-State Summary

| Case | Decision counts | Repair/block count | Primary servers | Live schema coverage | Notes |
|---|---|---:|---|---|---|
| `mcp_single_146` |  |  | csm, itsm, teams |  |  |
| `mcp_single_115` |  |  | gitea, itsm, teams |  |  |
| `mcp_single_72` |  |  | csm, hr, itsm, teams |  |  |
| `mcp_single_137` |  |  | csm, hr, itsm, teams |  |  |
| `mcp_single_145` |  |  | csm, itsm, teams |  |  |
| `mcp_single_143` |  |  | csm, itsm, teams |  |  |
| `mcp_single_52` |  |  | calendar, csm, email, itsm |  |  |
| `mcp_single_61` |  |  | calendar, csm, email, itsm |  |  |
| `mcp_single_64` |  |  | calendar, csm, email, itsm |  |  |
| `mcp_single_67` |  |  | calendar, csm, email, itsm |  |  |
| `mcp_single_151` |  |  | drive, gitea, itsm |  |  |
| `mcp_single_87` |  |  | calendar, email, hr, itsm |  |  |

## S2 Alignment Summary

| Case | Expected S2 focus | Observed S2 signal | Aligned? | Notes |
|---|---|---|---|---|
| `mcp_single_146` | false-block control |  |  |  |
| `mcp_single_115` | false-block control, Gitea tool_state |  |  |  |
| `mcp_single_72` | token-heavy false-block control |  |  |  |
| `mcp_single_137` | token-heavy false-block control |  |  |  |
| `mcp_single_145` | closure, timeout boundary |  |  |  |
| `mcp_single_143` | closure, timeout boundary |  |  |  |
| `mcp_single_52` | closure, tool_state |  |  |  |
| `mcp_single_61` | closure, tool_state, token-heavy failure |  |  |  |
| `mcp_single_64` | closure, tool_state |  |  |  |
| `mcp_single_67` | tool_state |  |  |  |
| `mcp_single_151` | Drive/Gitea/ITSM tool_state |  |  |  |
| `mcp_single_87` | HR/calendar/email tool_state |  |  |  |

## Claim Boundary

Can claim:

- Fresh S0 behavior for the fixed-12 suite under the fixed timeout and serial
  runner settings, if S0 was actually rerun.
- Near-online/post-run S2 diagnostic findings for closure and tool_state over
  the saved S0 artifacts.
- Closure improvements only when missing actions/evidence are explicitly
  identified from ground truth plus trace/timeout boundaries.
- Tool_state improvements only when findings are based on live-schema-normalized
  observed calls or concrete object-state evidence.
- False-block risk on pass controls when S2 repairs or blocks a clean S0 pass.

Must not claim:

- Full online S2 interception.
- Full online handoff enforcement unless structured handoff packets were
  intercepted before delegate dispatch.
- Raw provider latency, benchmark timeout, or token reduction as direct
  ContextHub wins.
- Judge-only semantic mismatches unless they map to concrete closure,
  tool_state, object-state, provenance, or handoff evidence.
- Any result from changed external EntCollabBench source or changed
  `src/contexthub/enforcement/`.

## Remaining Questions

- Did any pass control receive an S2 repair/block?
- Did live schema extraction fail for any server/tool?
- Were timeout cases converted into explicit missing closure obligations?
- Are Drive/Gitea findings comparable to CSM/ITSM findings, or do they need a
  separate stratum claim?
- Should a second fixed suite add more Drive/Gitea pass controls after this run?
