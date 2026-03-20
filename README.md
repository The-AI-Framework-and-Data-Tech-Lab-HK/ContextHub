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

Phase 1 用例说明见 `AMC_plan/13-phase1-test-design.md`。

## 依赖变更记录

| 日期 | 变更 |
|------|------|
| 初始 | 见 `pyproject.toml` 中 `project.dependencies` 与 `optional-dependencies.dev` |

后续在 `pyproject.toml` 中增加或升级依赖后，请在本表追加一行，并执行 `uv lock`（若使用 uv）。
