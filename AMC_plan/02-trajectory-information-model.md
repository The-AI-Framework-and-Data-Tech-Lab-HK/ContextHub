# 02 — 轨迹信息模型与计算图定义

## 2.1 输入轨迹现状（基于 sample_traj）

当前样例轨迹包含以下字段：

- `Step`
- `Thinking`
- `Action`
- `Action_result`
- `Response`
- `meta.role`（如 `AIMessage` / `ToolMessage`）

特点：
- 轨迹通常呈“AIMessage（thinking+action） -> ToolMessage（上一 action 的结果）”交替；
- 存在空字段、重复动作、失败动作、修正重试；
- `Action` 常为字符串表达（如 `tool_name(arg=...)`），需解析参数。

因此在 AMC 计算图里，**一个动作节点 = 一次 AIMessage 动作单元 + 其配对的 ToolMessage 输出**（若缺失 ToolMessage，则该节点输出为空并标记 `pending_output`）。

## 2.2 规范化实体定义

```python
class Trajectory:
    trajectory_id: str             # 轨迹唯一 ID（主键）
    tenant_id: str                 # 租户隔离 ID（索引/权限过滤关键字段）
    agent_id: str                  # 产生该轨迹的 agent ID
    task_type: str | None          # 任务类型标签（用于检索过滤与 workflow 分桶）
    task_summary_l0: str           # 轨迹 L0 摘要（~100 tokens，快速召回）
    task_overview_l1: str          # 轨迹 L1 概览（~1-2k tokens，重排/理解）
    status: str                    # 任务结果状态：success | partial | failed
    graph_backend_ref: dict        # 图后端定位信息（backend/graph_id/namespace 等）
    started_at: datetime           # 任务开始时间
    finished_at: datetime | None   # 任务结束时间（进行中可为空）
    metadata: dict                 # 扩展元数据（来源系统、标签、附加上下文）
```

```python
class ActionNode:
    node_id: str                   # 节点唯一 ID（图后端主键）
    trajectory_id: str             # 所属轨迹 ID
    ai_step: int                   # 对应 AIMessage 的 Step 序号
    tool_step: int | None          # 配对 ToolMessage 的 Step（缺失时为空）
    tool_name: str | None          # 工具名（如 local_db_sql）
    tool_args: dict | None         # 工具入参（结构化后）
    tool_output: dict | str | None # 工具输出（结构化对象或原始文本）
    output_status: str | None      # 节点执行状态：success | failed | partial | unknown
    thinking: str | None           # AI 的思考文本（可选，MVP 可只保留 raw_refs 回源）
    response: str | None           # AI 的自然语言响应（可选，MVP 可只保留 raw_refs 回源）
    raw_refs: list[str]            # 原始片段引用（AIMessage + ToolMessage，供回放/审计）
    quality_flags: list[str]       # 质量标签（如 parse_error/low_confidence）
    # 说明：当前阶段不强制生成 node-level abstract/overview（后续可扩展）
```

```python
class DependencyEdge:
    edge_id: str                   # 边唯一 ID
    trajectory_id: str             # 所属轨迹 ID
    src_node_id: str               # 源节点 ID（依赖提供方）
    dst_node_id: str               # 目标节点 ID（依赖使用方）
    dep_type: str                  # 依赖类型：dataflow | reasoning | retry | temporal
    signal: str | None             # 依赖证据（可选；MVP 可先不落，后续用于可解释增强）
    confidence: float              # 边置信度（0~1）
    signal_detail: dict | None     # 结构化证据（如 matched_tokens/reason_summary/source）
```

## 2.3 依赖边判定规则（MVP）

### Rule-1：先做“节点配对”，不做节点间边
若某 `ToolMessage` 紧邻前序 `AIMessage`，将其合并进同一个 `ActionNode`（动作与输出属于同一节点内部信息，不再单独建边）。

### Rule-2：节点间 dataflow 依赖
在后续节点 `tool_args` 中匹配前序节点 `tool_output` 的关键字段/值（如表名、列名、SQL 片段、文件路径），建立 `dataflow` 边。

补充示例：
- A 节点 `tool_output.data` 返回表名列表（如 `["ch___company_info", ...]`）；
- B 节点 `tool_args.command` 中出现 `PRAGMA table_info(ch___company_info)`；
- 判定 A -> B 为 `dataflow`（`signal=evidence_type: enum_to_command`）。

### Rule-3：失败-修正链
若前序节点 `output_status=failed`，且后续出现同工具修正调用，建立 `retry` 边（`signal=retry`）。

### Rule-4：时间顺序兜底
若无法识别“前序输出被后续输入消费”的证据，则创建低置信度 `temporal` 边以避免图断裂。

### Rule-5：思维推理依赖（reasoning）
当节点 `B.thinking` 明确参考了前序节点 `A` 的执行结果（通常来自 `A.tool_output`）时，建立 `A -> B` 的 `reasoning` 有向边。

口径：
- `reasoning` 表示“思考依据依赖”，不是输入参数消费关系；
- `dataflow` 与 `reasoning` 可同时存在于同一对节点（语义不同，不互斥）；
- `reasoning` 的证据说明建议落在 `signal_detail.reason_summary`。

口径要求：
- `dep_type=dataflow`：表示“真实 output->input 依赖”；
- `dep_type=reasoning`：表示“B.thinking 参考了 A 的执行结果”；
- `dep_type=temporal`：仅表示顺序相邻/时序兜底，不可等价为数据依赖；
- 下游检索与传播必须按 `dep_type` 做差异化权重或过滤。

## 2.4 双版本计算图（raw / clean）

为支持“完整可审计”与“高质量检索”两类目标，AMC 对同一条轨迹维护两版图：

- **Raw Graph（原始图）**：保留所有节点和边（包括失败调用、重试、回滚、无效分支），用于审计、诊断、失败模式学习。
- **Clean Graph（清洗图）**：在 Raw Graph 基础上清理失败噪音，仅保留“有效完成路径”的节点与依赖，用于主检索与 workflow 抽象。

清洗规则（MVP）：
1. 保留最终成功链路上的节点；
2. 对失败后被修正替代的节点，默认从 clean 图移除；
3. 若失败节点承载关键信息（如失败原因模板），可保留为注释节点或 side-branch，但不参与主路径打分；
4. raw 与 clean 通过 `origin_node_id` 建立映射，可双向跳转回溯。

图存储约束：
- **raw/clean 两版图全部存储在图后端**（如 Neo4j），不落文件系统；
- 文件系统只存储轨迹级元信息与图后端指针（用于跳转查询）。

## 2.5 分层内容表示（借鉴 L0/L1/L2）

- **Trajectory-L0**：任务一句话摘要 + 关键工具序列；
- **Trajectory-L1**：任务目标、主要步骤、关键分叉与失败修复；
- **Trajectory-L2**：轨迹原始步骤引用（可回放）。

当前阶段：
- 只要求 **trajectory-level L0/L1**；
- **node-level abstract/overview 暂不生成**，后续按检索效果再增量引入。

## 2.6 存储键空间建议

```
ctx://agent/{agent_id}/memories/trajectories/{trajectory_id}/
  ├── trajectory.json              # 轨迹级元信息
  ├── graph_pointer.json           # 指向图后端（raw/clean graph id）
  ├── raw_steps.jsonl
  ├── .abstract.md
  ├── .overview.md
  ├── .deps.json
  └── audit.log
```

文件职责说明：
- `trajectory.json`：轨迹元信息（状态、标签、graph 引用摘要）。
- `graph_pointer.json`：图后端定位入口（raw/clean graph id）。
- `raw_steps.jsonl`：原始逐步轨迹快照（便于回放与审计）。
- `.abstract.md/.overview.md`：仅 trajectory-level 的 L0/L1 索引文本。
- `.deps.json`：正向依赖声明（skill/table/tool/trajectory）。
- `audit.log`：本轨迹范围内的关键操作审计记录。

`graph_pointer.json` 示例：
```json
{
  "backend": "neo4j",
  "namespace": "tenant_acme",
  "raw_graph_id": "traj_123_raw",
  "clean_graph_id": "traj_123_clean",
  "origin_mapping_ref": "neo4j://.../mapping/traj_123"
}
```

团队共享轨迹或抽象 workflow 走 `ctx://team/...` 路径（按审核与 ACL 策略控制）；但图实体依然落图后端，不在文件系统重复存储。

