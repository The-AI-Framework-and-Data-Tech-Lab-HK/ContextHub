# EntCollabBench Artifact Layout

This file defines the local Phase 4 artifact layout. Keep it aligned with `README.md`, `PILOT_RUNBOOK.md`, and `runs/rerun_protocol.md`.

## Canonical Roots

- ContextHub repo: `/Users/sherrylin/Documents/PythonProjects/ContextHub`
- EntCollabBench external clone: `/Users/sherrylin/Documents/PythonProjects/public/EntCollabBench`
- ContextHub run ledger: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs`
- EntCollabBench raw benchmark output: `/Users/sherrylin/Documents/PythonProjects/public/EntCollabBench/scripts/result`
- Fixed experiment definitions: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/experiments/fixed12`

## Roles

`experiments/fixed12/` contains experiment definitions, not benchmark run output.

`runs/` is the canonical ledger. Read historical data from `runs/registry.jsonl` or from `runs/<run_id>/manifest.json`. Each manifest points to the raw result, trajectory, and decision-log evidence.

`public/EntCollabBench/scripts/result/` is the native raw output area for EntCollabBench benchmark artifacts. Do not infer paper eligibility by scanning this directory.

`.smoke_observe/` and `.smoke_full_s2/` contain historical online proxy decision logs. New formal runs should use `runs/<run_id>/` instead of writing new formal artifacts into these smoke workspaces.

## New Run Rule

Every new experiment must:

1. Create `runs/<run_id>/manifest.json`.
2. Write `result.path`, `trajectory.path`, and `decision_log.path` when those artifacts exist.
3. Append one row to `runs/registry.jsonl`.
4. Record whether the run is `paper_eligible` and why.
5. Keep API keys and secret values out of all artifacts.

Raw result and trajectory files may stay in `public/EntCollabBench/scripts/result`, but the run manifest must point to their exact paths. Do not move unique historical result, trajectory, or decision-log files just to make the tree look cleaner.

