# EntCollabBench Fixed Cases Runner Prompt

Use this prompt when delegating the first fixed-case pilot to another runner
agent. The goal is a fresh S0 benchmark followed by a near-online/post-run S2
diagnostic over the same fixed cases. This is not a full online S2 run.

```text
You are running the ContextHub x EntCollabBench first fixed-case pilot.

Repositories:
- ContextHub: /Users/sherrylin/Documents/PythonProjects/ContextHub
- EntCollabBench external clone: /Users/sherrylin/Documents/PythonProjects/research/EntCollabBench

Read first:
- /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/PILOT_RUNBOOK.md
- /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/experiments/fixed12/FIXED_CASES_MANIFEST_12.json
- /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/RUNTIME_WRAPPER_NOTES.md
- /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/experiments/fixed12/FIXED_CASES_REPORT_TEMPLATE.md

Hard rules:
- Do not print, inspect, or expose API keys or secret values.
- Do not create git commits.
- Do not modify external EntCollabBench source code.
- Do not modify src/contexthub/enforcement/.
- Do not use random sampling.
- Do not expand, shrink, or reorder the fixed case list unless explicitly asked.
- Do not run full online S2 or describe the diagnostic as full online.
- Do not overwrite existing reports or result files.

Experiment boundary:
- Run a fresh S0 benchmark using the fixed cases in
  `experiments/fixed12/FIXED_CASES_MANIFEST_12.json`.
- Then run ContextHub's near-online/post-run S2 diagnostic on the saved S0 result
  and trajectory artifacts.
- S2 tool_state findings are diagnostics over observed trace events, with live
  MCP schema normalization where available.
- S2 closure findings are diagnostics at clean result, error, timeout, or partial
  trace boundaries.
- Handoff is partial unless the harness exposes structured handoff packets; do
  not claim full online handoff interception.

Fixed runtime settings:
- TASK_TIMEOUT_SECONDS=1000
- AGENT_HTTP_TIMEOUT_SECONDS=400
- JUDGE_TIMEOUT_SECONDS=500
- --batch-concurrency 1
- --continue-on-error
- NO_PROXY/no_proxy must include 127.0.0.1 and localhost for local services.

Output path rules:
- Create a fresh timestamped directory under:
  /Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs/
- Directory name format:
  fixed12_s0_s2_YYYYMMDD_HHMMSS
- Put copied/generated fixed task specs under:
  <run_dir>/specs/
- Put S0 benchmark outputs under:
  <run_dir>/s0/
- Put near-online/post-run S2 outputs under:
  <run_dir>/s2/
- Put the final filled report under:
  <run_dir>/FIXED12_PILOT_REPORT.md
- Never overwrite an existing directory or report. If the target exists, create a
  new timestamped directory.

Suggested execution order:
1. Confirm Docker, MCP services, and agent services are healthy.
2. Load the intended model env from ContextHub without printing secret values.
3. Materialize one fixed task spec per manifest case.
4. Run S0 benchmark serially over the fixed specs with the fixed timeouts above.
5. Record S0 result paths, trajectory paths, status, pass/fail/timeout, tokens,
   duration, cleanup/memory failures, and export-state compatibility issues.
6. Run the ContextHub near-online/post-run S2 diagnostic against the saved S0
   artifacts.
7. Fill `experiments/fixed12/FIXED_CASES_REPORT_TEMPLATE.md` into the final report path.

Stop conditions:
- Stop immediately if any API key or secret value would need to be printed.
- Stop if Docker/MCP/agent health is not clean after one reasonable restart or
  remediation attempt; report the failing service and evidence.
- Stop if any case exceeds TASK_TIMEOUT_SECONDS=1000 or the runner appears hung
  beyond the configured benchmark timeout plus a small cleanup margin.
- Stop if one case exceeds 1,800,000 run tokens, or if two consecutive cases
  exceed 1,200,000 run tokens.
- Stop if S0 outputs or trajectories cannot be written to the timestamped run
  directory.
- Stop if the S2 diagnostic attempts to modify external EntCollabBench source or
  src/contexthub/enforcement/.
- Stop rather than improvising if task specs cannot be materialized
  deterministically from the manifest.

Required return fields:
- run_dir
- manifest_path
- case_ids
- s0_result_paths
- s0_trajectory_paths
- s2_summary_path
- final_report_path
- s0_distribution: passed / failed / timeout / stopped counts
- per_case_status: case_id, status, task_passed, failed_agents, failure_categories
- tokens_and_duration: case_id, run_total_tokens, judge_total_tokens,
  duration_seconds, high_cost
- s2_summary: case_id, closure_verdict, missing_actions, open_questions,
  tool_state_decision_counts, repair_or_block_count, alignment_notes
- stop_condition_triggered: true/false
- stop_reason
- claim_boundary: what can be claimed, what must not be claimed
- remaining_questions
```

## Runner Notes

- The first fixed suite is in `experiments/fixed12/FIXED_CASES_MANIFEST_12.json`.
- The report template is `experiments/fixed12/FIXED_CASES_REPORT_TEMPLATE.md`.
- Existing reports, including `ONLINE_S2_PILOT_REPORT.md`, are prior artifacts
  and must not be overwritten.
- If only S2 diagnostics are run from existing S0 artifacts, label the result
  clearly as "near-online/post-run S2 diagnostic only"; do not call it a fresh
  S0 benchmark.
