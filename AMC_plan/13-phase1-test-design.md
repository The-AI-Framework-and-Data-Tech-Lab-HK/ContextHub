# 13 — Phase 1 测试设计（基础数据模型 + Commit 可用）

本文档承接 `09-implementation-plan.md` **Phase 1** 与 `02/03` 规范，结合仓库内 `sample_traj/*.json` 样例，定义**第一阶段应覆盖的测试范围、用例分层与验收口径**。  
**Phase 1 不测 retrieve**（属 Phase 2）；本阶段以 **commit 全链路 + 单轨迹回放/详情查询**为主。

---

## 13.1 Phase 1 功能范围（来自 09）

| # | 能力 | 规范依据 | 测试关注点 |
|---|------|----------|------------|
| 1 | trajectory / node / edge 数据模型与校验 | `02-trajectory-information-model.md` | Pydantic/JSON Schema、必填字段、ID 确定性 |
| 2 | commit API + 校验 + 幂等 | `03-commit-pipeline.md`、`09` | HTTP 契约、错误码、重复提交行为 |
| 3 | 规则版图构建（raw + clean） | `02` §2.4、`03` §3.3–3.4 | 配对、dataflow/controlflow(retry)/temporal、环标注 |
| 4 | Graph Store 写入 + FS `graph_pointer` | `03` §3.6、`10` | Neo4j 节点/边属性、指针 JSON 可解析 |
| 5 | trajectory-level L0/L1 + 向量索引 | `03` §3.5、`12` | 仅 L0/L1；IndexDoc 无冗余字段；upsert 幂等 |
| 6 | 审计日志 | `05`、`03` | commit 写审计、敏感字段脱敏/摘要 |

**里程碑 M1**（09）：可稳定提交并查询单条轨迹详情；**图构建成功率 > 95%（样例集）**。

---

## 13.2 `sample_traj` 样例特征（用于选例与断言）

仓库路径：`sample_traj/traj{1..5}.json`。均为 **AIMessage ↔ ToolMessage** 交替的 SQL 分析类任务，适合覆盖配对、数据流与失败-重试。

| 文件 | 典型特征 | 建议覆盖的测试意图 |
|------|----------|---------------------|
| `traj1.json` | 英文思考；含 **Tool 失败**（如表名含 `&` 导致语法错误）及 **后续修正成功** | raw 保留失败边；clean 主路径；`controlflow(retry)`；`failure_signature` 相关（若实现） |
| `traj2.json` | 中文思考；多步探索 + 长 `Action_result` | 规范化截断/摘要；dataflow 跨表名；编码 UTF-8 |
| `traj3.json` | 连续多步 **仅有 AIMessage、无紧随 ToolMessage**（配对缺口） | `pending_output`、质量标签、图不崩溃 |
| `traj4.json` | 中文；步数多、结构复杂 | 大图构建性能与稳定性；temporal 兜底边数量上界（可选） |
| `traj5.json` | 中英文混合；**标准 AIMessage→ToolMessage** 节奏 | 「黄金路径」冒烟；作为幂等/回归基线 |

**样例集定义（M1 统计口径）**：默认五文件全跑；若 CI 无 Neo4j，则仅跑 **不依赖图后端** 的单元测试，集成测试标记 `slow`/`integration` 跳过。

---

## 13.3 测试分层

### A. 单元测试（`src/tests/unit/`，无外部服务）

| 编号 | 模块/行为 | 输入fixture | 断言要点 |
|------|-----------|-------------|----------|
| U-01 | JSON 加载与 Step 单调 | `sample_traj/traj1.json` | 数组非空；`Step` 严格递增；`meta.role` 合法 |
| U-02 | 配对 `pairing` | traj1 / traj5 | 每个 AIMessage 与下一 ToolMessage 成对；traj3 识别缺口 |
| U-03 | `normalizer`：Action 字符串解析 | traj1 中 `local_db_sql(...)` | 解析出 `tool_name`、`file_path`、`command` |
| U-04 | `normalizer`：超长 `Action_result` | traj2 中长 result | 截断后仍有摘要或引用；不超过配置上限 |
| U-05 | `graph_builder` raw | traj1 | 失败节点存在；retry 边存在（与实现命名一致） |
| U-06 | `clean_deriver` | traj1 | clean 节点数 ≤ raw；失败被替代路径在 clean 中弱化或移除（按 02 规则） |
| U-07 | 边类型 | traj1 + traj2 | 至少存在 temporal 或 dataflow；traj1 存在 retry 类 controlflow |
| U-08 | 确定性 ID | 同一轨迹两次构建 | 相同输入 → 相同 `trajectory_id`（若由输入哈希决定）或相同 `node_id` 规则 |
| U-09 | 幂等键 | 相同 `tenant_id+task_id+trajectory` | 第二次 commit 返回 **idempotent** 或相同 `trajectory_id`（与 API 设计一致） |

### B. 集成测试（`src/tests/integration/`，需 Neo4j / Chroma / 本地 FS）

| 编号 | 场景 | 依赖 | 断言要点 |
|------|------|------|----------|
| I-01 | `POST /api/v1/amc/commit` 成功 | FastAPI + 真实或 testcontainer Neo4j + Chroma + temp 目录 | `status=accepted`；`nodes/edges` > 0；HTTP 201/200 |
| I-02 | FS 落盘 | 同上 | 存在 `trajectory.json`、`.abstract.md`、`.overview.md`、`graph_pointer.json`；URI 形态符合 `ctx://agent/{agent_id}/memories/trajectories/{id}/` 映射（实现层路径等价即可） |
| I-03 | Neo4j raw/clean | Neo4j | 可查询到 `graph_kind=raw|clean`（或等价标签/属性）；边含 `dep_type`、`confidence` |
| I-04 | Chroma 索引 | Chroma + Embedding（可用 fake embedding 注入） | collection 中存在 doc id 与 `tenant_id` metadata；重复 commit 不重复脏行 |
| I-05 | 审计 | 文件 audit sink | commit 产生一条审计；`query_text` 按策略 redact |
| I-06 | 单轨迹详情/回放 | `GET .../replay/{trajectory_id}` 或内部 repo | 能读回与 sample 一致的 step 序列或 raw_refs |

### C. 样例集「构建成功率」验收（M1）

- **定义**：对 `traj1`–`traj5` 各执行一次完整 commit（集成环境），`status=accepted` 且图构建无异常则计为成功。
- **目标**：\(\ge 95\%\) → 五文件中至多允许 **0** 失败（100%）；若后续扩充样例至 20 条，则允许 1 条失败。
- **实现方式**：单独测试 `test_m1_sample_set_graph_build_success_rate` 或使用参数化 `pytest.mark.parametrize("traj_file", [...])` + 最后汇总。

---

## 13.4 测试数据与 Fixture 约定

- **路径常量**：`PROJECT_ROOT / "sample_traj" / "traj{n}.json"`（避免复制大 JSON 进测试代码）。
- **请求封装**：将 `list[dict]` 包装为 commit body：`tenant_id`、`agent_id`、`session_id`、`task_id`、`labels.task_type`（如 `sql_analysis`）、`is_incremental=false`。
- **`task_id` 策略**：每个文件使用稳定唯一 `task_id`（如 `task-traj1-sample`），避免与幂等测试冲突；幂等测试需固定 `task_id` 与轨迹内容。

---

## 13.5 显式不包含（避免 Phase 1 范围膨胀）

- 语义召回、图召回、rerank、evidence、ACL/Mask 全链路（**Phase 2**）。
- feedback、propagation、lifecycle（**Phase 3**）。
- workflow 聚类（**Phase 4**）。

---

## 13.6 建议的 pytest 标记

| 标记 | 含义 |
|------|------|
| `@pytest.mark.unit` | 纯逻辑，默认 CI 必跑 |
| `@pytest.mark.integration` | 需要 Neo4j/Chroma/网络 |
| `@pytest.mark.m1` | M1 里程碑验收用例 |

---

## 13.7 与代码目录的对应关系

| 测试目录 | 被测包（见 `10-main-code-structure.md`） |
|----------|------------------------------------------|
| `tests/unit/commit/*` | `core/commit/*`（避免使用 `tests/unit/core/` 目录名，以免遮蔽源码包 `core`） |
| `tests/unit/domain/*` | `domain/models/*`、`domain/value_objects/*` |
| `tests/integration/api/*` | `api/routes/commit.py`、`app/orchestrators/commit_orchestrator.py` |
| `tests/integration/infra/*` | `infra/storage/graph/*`、`infra/storage/vector/*`、`infra/storage/fs/*` |

---

## 13.8 后续落地顺序（实现测试代码时）

1. 先写 **U-01–U-03**（fixture + 配对 + 解析），不依赖服务。  
2. 再写 **U-05–U-07**（图构建），可用内存图或 mock `GraphStore`。  
3. 打通 **I-01–I-03**（真实存储），最后 **I-04**（向量，可 mock embedder）。  
4. 收尾 **M1 样例集成功率** 与审计 **I-05**。

该文档用于指导 `src/tests/` 下用例实现；若与 `02/03` 冲突，以 `AMC_plan` 规范文档为准。
