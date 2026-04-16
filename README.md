# ContextHub — AMC（Agent Memory Core）

轨迹记忆子系统：commit（解析轨迹 → raw/clean 图 → 持久化）与 retrieve（语义 + 图相似 + 融合）。  
设计文档见目录 `AMC_plan/`。

## 配置

| 路径 | 说明 |
|------|------|
| `config/config.yaml` | 非敏感参数（默认 AMC 配置） |
| `.env.example` | 环境变量模板 |
| `.env` | 本地填写密钥；**已 `.gitignore`，勿提交** |

合并优先级与字段说明见 `AMC_plan/12-configuration-spec.md`。

## 环境

- Python **≥ 3.11**
- 推荐使用虚拟环境（示例使用项目内 `.venv`）

## 基础服务依赖（pgvector + Neo4j）

AMC 的 commit/retrieve 集成测试与本地完整链路依赖以下服务：

- PostgreSQL（启用 `pgvector` 扩展）
- Neo4j（图存储）

说明（重要）：
- 若 PostgreSQL 未启动，`pgvector` 向量检索/索引会被自动禁用；
- 若 Neo4j 未启动，图存储与图召回分支会被自动禁用；
- 要验证完整链路（commit + retrieve + graph/vector），这两个服务都必须处于运行状态。

参考：
- [How to install PostgreSQL with pgvector on Ubuntu - Rocketeers](https://rocketee.rs/install-postgresql-pgvector-ubuntu)
- [How to install Neo4j on Ubuntu Server - TechRepublic](https://www.techrepublic.com/article/how-to-install-neo4j-ubuntu-server/)

### Ubuntu 安装 PostgreSQL + pgvector

```bash
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt install -y postgresql postgresql-17-pgvector
sudo systemctl enable postgresql
sudo systemctl start postgresql
```

启用扩展（以 `postgres` 用户执行）：

```bash
sudo -u postgres psql
CREATE EXTENSION IF NOT EXISTS vector;
\q
```

可选：创建 AMC 专用库与用户（示例）：

```bash
sudo -u postgres psql
CREATE USER amc_user WITH PASSWORD 'amc_password';
CREATE DATABASE amc_db OWNER amc_user;
\c amc_db
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL PRIVILEGES ON DATABASE amc_db TO amc_user;
\q
```

连通性验证：

```bash
psql "postgresql://amc_user:amc_password@127.0.0.1:5432/amc_db" -c "SELECT extname FROM pg_extension WHERE extname='vector';"
```

### Ubuntu 安装 Neo4j

安装并启动（APT 方式）：

```bash
sudo apt update
sudo apt install -y neo4j
sudo systemctl enable neo4j
sudo systemctl start neo4j
```

确认服务状态：

```bash
sudo systemctl status neo4j
```

首次安装后请按 Neo4j 提示完成初始密码设置，然后更新 `.env` 中 `AMC_NEO4J_*` 配置。

### 与 AMC 配置对齐

在 `.env`（或 `config/config.yaml`）中至少配置：

```bash
AMC_VECTOR_STORE_BACKEND=pgvector
AMC_PGVECTOR_DSN=postgresql://amc_user:amc_password@127.0.0.1:5432/amc_db
AMC_PGVECTOR_SCHEMA=public
AMC_PGVECTOR_TABLE=amc_embeddings

AMC_GRAPH_STORE_BACKEND=neo4j
AMC_NEO4J_URI=bolt://127.0.0.1:7687
AMC_NEO4J_USER=neo4j
AMC_NEO4J_PASSWORD=your_password
AMC_NEO4J_DATABASE=neo4j
```

### 手动服务控制（可选，排障用）

推荐（systemd）：

```bash
sudo systemctl start postgresql
sudo systemctl start neo4j
sudo systemctl status postgresql
sudo systemctl status neo4j
```

兼容（service 命令）：

```bash
sudo service postgresql start
sudo service neo4j start
sudo service postgresql status
sudo service neo4j status
```

端口自检（应为 `True`）：

```bash
python - <<'PY'
import socket
for p in (5432, 7687):
    s = socket.socket(); s.settimeout(0.8)
    ok = False
    try:
        s.connect(("127.0.0.1", p))
        ok = True
    except Exception:
        ok = False
    finally:
        s.close()
    print(p, ok)
PY
```

## 一键安装依赖

**依赖清单以根目录 `pyproject.toml` 为准**（运行时 + 可选开发组 `[dev]`）。

### 使用 uv（推荐，可锁版本）

```bash
uv sync                    # 安装运行时依赖 + 可编辑包
uv sync --extra dev        # 含 pytest / ruff / mypy 等
# 若仓库中已有 uv.lock，uv sync 会按锁文件解析
```

本地更新锁文件（新增/升级依赖后执行）：

```bash
uv lock
```

### 使用 pip

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e ".[dev]"     # 可编辑安装 + 开发依赖
```

安装后可在 Python 中导入顶层包：`api`、`core`、`domain`、`infra`、`app`（源码位于 `src/`）。

## 测试

```bash
pytest src/tests -m "not integration"   # 单元测试（默认 CI）
pytest src/tests -m integration          # 集成测试（需 Neo4j 等，部分用例仍为占位 skip）
```

### FastAPI 功能脚本（拆分版）

统一使用一个命令启动整套服务（PostgreSQL + Neo4j + AMC API）：

```bash
bash scripts/start_amc.sh
```

该脚本会先检查并启动：
- PostgreSQL（5432）
- Neo4j（7687）

并自动执行 Python 环境准备：
- 自动创建/激活 `.venv`（若不存在）；
- 自动执行 `pip install -U pip`；
- 若检测到依赖缺失，自动执行 `pip install -e ".[dev]"`。

然后再启动 AMC API（`uvicorn main:app --app-dir src ...`）。

可选：通过环境变量调整监听地址/端口（仍由同一脚本启动）：

```bash
AMC_HOST=0.0.0.0 AMC_PORT=8000 AMC_RELOAD=1 bash scripts/start_amc.sh
```

可选：若你明确不希望脚本触发 pip 安装（例如 CI 或离线环境），可设置：

```bash
AMC_INSTALL_DEPS=0 bash scripts/start_amc.sh
```

再分别测试 commit / promote / retrieve：

```bash
python scripts/test_commit_api.py --pretty
python scripts/test_promote_api.py --trajectory-id <committed_trajectory_id> --pretty
python scripts/test_retrieve_api.py --pretty
```

`commit` 脚本（默认超时 600s）示例：

```bash
python scripts/test_commit_api.py \
  --base-url "http://127.0.0.1:8000/api/v1/amc" \
  --health-url "http://127.0.0.1:8000/healthz" \
  --account-id acc-demo \
  --agent-id agent-a \
  --trajectory-file sample_traj/traj5.json \
  --commit-timeout 600 \
  --pretty
```

`retrieve` 脚本（默认超时 600s）示例：

```bash
python scripts/test_retrieve_api.py \
  --base-url "http://127.0.0.1:8000/api/v1/amc" \
  --health-url "http://127.0.0.1:8000/healthz" \
  --account-id acc-demo \
  --agent-id agent-a \
  --partial-trajectory-file sample_graph_query/pq04_pending_output_traj5.json \
  --task-description "中小微 企业信贷及经营数据" \
  --tool-whitelist local_db_sql \
  --retrieve-timeout 600 \
  --top-k 5 \
  --pretty
```

`promote` 脚本示例：

```bash
python scripts/test_promote_api.py \
  --base-url "http://127.0.0.1:8000/api/v1/amc" \
  --health-url "http://127.0.0.1:8000/healthz" \
  --account-id acc-demo \
  --agent-id agent-a \
  --trajectory-id traj_xxx \
  --target-team engineering \
  --reason "promote reusable workflow for cross-agent demo" \
  --pretty
```

### Demo 串联：commit -> promote -> retrieve

可用如下顺序验证“一个 agent 存并晋升，另一个 agent 复用检索”：

```bash
# 1) agent-a 提交轨迹
python scripts/test_commit_api.py \
  --account-id acc-demo \
  --agent-id agent-a \
  --trajectory-file sample_traj/traj1.json \
  --task-id task-funnel-v1 \
  --pretty

# 2) 把上一步返回的 trajectory_id 晋升到 team 空间
python scripts/test_promote_api.py \
  --account-id acc-demo \
  --agent-id agent-a \
  --trajectory-id traj_xxx \
  --target-team engineering \
  --pretty

# 3) agent-b 在相似任务下检索（team 作用域）
python scripts/test_retrieve_api.py \
  --account-id acc-demo \
  --agent-id agent-b \
  --task-description "funnel diagnosis and strategy planning for growth campaign" \
  --top-k 5 \
  --pretty
```

Phase 1 用例说明见 `AMC_plan/13-phase1-test-design.md`。
运行与部署方式见 `docs/run-and-test.md`。
AMC v0 作为 OpenClaw context engine 的接入步骤见 `docs/amc-openclaw-integration-guide.md`。

补充说明：
- `--account-id` 是当前主参数；
- 账号上下文统一使用 `account_id`（或 `X-Account-Id` 请求头）。
- 检索默认先做语义召回（L0/L1）；当提供 `partial_trajectory` 且图后端可用时，会追加图相似召回并做融合打分；不可用时自动回退语义召回并在 warnings 标注原因。

## 命令行提交轨迹（无 HTTP）

```bash
amc-commit-trajectory sample_traj/traj1.json \
  --account-id acc-demo \
  --agent-id agent-a \
  --scope agent \
  --owner-space agent-a \
  --pretty
```

该命令会直接运行 Phase 1 commit pipeline，并输出 L0/L1 与图文件落盘位置。
如需在同目录生成 `raw_graph.png` 和 `clean_graph.png`，可增加参数：

```bash
amc-commit-trajectory sample_traj/traj1.json \
  --account-id acc-demo \
  --agent-id agent-a \
  --scope agent \
  --owner-space agent-a \
  --visualize-graph-png \
  --pretty
```

## 依赖变更记录

| 日期 | 变更 |
|------|------|
| 初始 | 见 `pyproject.toml` 中 `project.dependencies` 与 `optional-dependencies.dev` |
| 2026-03 | README 补充 PostgreSQL/pgvector 与 Neo4j 的 Ubuntu 安装说明 |

后续在 `pyproject.toml` 中增加或升级依赖后，请在本表追加一行，并执行 `uv lock`（若使用 uv）。
