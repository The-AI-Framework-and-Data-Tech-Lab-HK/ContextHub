# sample_graph_query

Partial trajectory query cases for dual-path retrieve testing (semantic + graph).

All files follow the same JSON step format used in `sample_traj/`:
- `Step`
- `Thinking`
- `Action`
- `Action_result`
- `Response`
- `meta.role` (`AIMessage` / `ToolMessage`)

Suggested usage:
- `amc-retrieve-trajectory --partial-trajectory-file sample_graph_query/pq02_retry_pattern_traj1.json ...`
- `amc-retrieve-trajectory --partial-trajectory-file sample_graph_query/pq04_pending_output_traj5.json ...`

Case list:
- `pq01_schema_probe_traj1.json`: `traj1` prefix (`Step 1` -> `Step 4`), early schema discovery. Ground truth: `sample_traj/traj1.json`
- `pq02_retry_pattern_traj1.json`: `traj1` prefix (`Step 1` -> `Step 10`), includes failed SQL and retry. Ground truth: `sample_traj/traj1.json`
- `pq03_metric_aggregation_traj5.json`: `traj5` prefix (`Step 1` -> `Step 8`), KPI aggregation. Ground truth: `sample_traj/traj5.json`
- `pq04_pending_output_traj5.json`: `traj5` prefix (`Step 1` -> `Step 10`), mid-run progress before composite scoring. Ground truth: `sample_traj/traj5.json`
- `pq05_report_generation_traj5.json`: `traj5` prefix (`Step 1` -> `Step 14`), stops before report write completion. Ground truth: `sample_traj/traj5.json`
