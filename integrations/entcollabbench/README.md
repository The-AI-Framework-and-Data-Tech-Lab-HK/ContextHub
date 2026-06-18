# EntCollabBench Integration Notes

This package contains ContextHub-side mapping conventions only. It does not vendor EntCollabBench source code or datasets.

## Get EntCollabBench

Clone the upstream repository outside this ContextHub checkout:

```bash
mkdir -p /Users/sherrylin/Documents/PythonProjects/research
cd /Users/sherrylin/Documents/PythonProjects/research
git clone https://github.com/yutao1024/EntCollabBench.git
cd EntCollabBench
git rev-parse HEAD
```

Task 7 investigated commit `9d085fcb86adaf20254c09e2ca35123e535a9643`.

## Install

Upstream README asks for Linux, Docker + Docker Compose, Conda, and Python 3.11.

```bash
conda create -n EntCollabbench python=3.11
conda activate EntCollabbench
pip install -r requirements.txt
```

Task 7 could not run Docker on this machine because `docker` was not installed. A temporary venv in the external clone was enough to run `scripts/benchmark.py --help`, but not the full benchmark.

## Download Dataset Files

The upstream repo expects task files under `scripts/dataset/`:

```bash
mkdir -p scripts/dataset
python - <<'PY'
from pathlib import Path
from urllib.request import urlretrieve

base = "https://huggingface.co/datasets/Kirito-Lab/EntCollabBench/resolve/main"
files = [
    "mcp_tasks_160.json",
    "mcp_multi_tasks_40.json",
    "approval_tasks_80.json",
    "approval_multi_task_20.json",
]
out = Path("scripts/dataset")
for name in files:
    urlretrieve(f"{base}/{name}", out / name)
    print(name)
PY
```

## Run Services

Start MCP services first:

```bash
docker compose -f Arena/docker-compose-mcp.yml up -d --force-recreate
```

Then start the agent services:

```bash
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://<openai-compatible-host>/v1
export AGENT_LLM_MODEL=<model>
export AGENT_SUMMARY_MODEL=<model>
export TASK_TIMEOUT_SECONDS=1000
export AGENT_HTTP_TIMEOUT_SECONDS=400
export JUDGE_TIMEOUT_SECONDS=500

docker compose -f agent/docker-compose.yml build
docker compose -f agent/docker-compose.yml up -d --force-recreate
```

## Run One Sample Batch

The benchmark requires judge credentials even for a one-batch run:

```bash
export JUDGE_OPENAI_API_KEY=...
export JUDGE_OPENAI_BASE_URL=https://<openai-compatible-host>/v1
export JUDGE_MODELS=<judge-model>

python scripts/benchmark.py \
  --tasks-spec-file scripts/dataset/mcp_tasks_160.json \
  --sample-k 1 \
  --sample-seed 1 \
  --batch-concurrency 1 \
  --agent-url-map-file config/agent_url_map.json \
  --trajectory-full-mode \
  --bench-result-jsonl scripts/result/sample_result.jsonl \
  --trajectory-run-jsonl scripts/result/sample_traj.jsonl \
  --continue-on-error
```

If Docker or judge env is missing, do not infer results. Record the exact failing command and error in `ENTCOLLABBENCH_FINDINGS.md`.

## ContextHub Mapping

Use `integrations.entcollabbench.mapping` from ContextHub:

- store base URIs such as `ctx://entcollab/tool_schema/itsm` in `contexts.uri`
- use `ctx://entcollab/tool_schema/itsm@v3` only as a runtime ref in handoff/tool/closure payloads
- import `role_uri`, `tool_schema_uri`, `policy_uri`, `object_uri`, `resolve_version_tag`, `role_to_owner_space`, and `to_tool_contract_fields` in Task 8/9
