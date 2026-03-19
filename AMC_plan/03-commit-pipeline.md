# 03 — Commit 设计：轨迹入库与图构建

## 3.1 API 草案

```python
POST /api/v1/amc/commit

request = {
  "tenant_id": "...",
  "agent_id": "...",
  "session_id": "...",
  "task_id": "...",
  "trajectory": [...],           # 原始 steps
  "is_incremental": false,
  "labels": {"task_type": "sales_analysis"}
}
```

```python
response = {
  "trajectory_id": "...",
  "nodes": 42,
  "edges": 58,
  "status": "accepted",
  "warnings": []
}
```

请求/响应字段说明：
- `session_id`：上层会话标识，用于把多次增量 commit 串成同一上下文。
- `task_id`：业务任务标识（幂等检查关键字段之一）。
- `is_incremental`：是否为增量提交（true 时会尝试基于已有轨迹版本合并）。
- `labels.task_type`：任务类型标签，影响后续检索过滤与 workflow 抽象分桶。
- `nodes/edges`：本次提交后图规模统计（用于调试和监控）。
- `warnings`：非阻断异常提示（如部分 step 无法解析）。

## 3.2 Commit 流水线

```
Receive -> Validate -> Normalize -> Pair AI/Tool -> Build Raw Graph -> Build Clean Graph
        -> Generate Trajectory L0/L1 -> Persist (graph backend + fs pointer + vector)
        -> Register Deps -> Emit Events -> Audit
```

## 3.3 关键步骤

### (1) Validate
- JSON schema 校验（Step 单调、字段类型、meta.role）；
- tenant_id / agent_id 权限校验；
- 幂等键检查（`tenant_id + task_id + hash(trajectory)`）。

### (2) Normalize
- 将样例中的字符串 Action 解析为 `tool_name + args`；
- `Action_result` 过长内容切片，保留摘要与原文引用；
- 统一时间与编码，补齐缺失字段默认值。

### (3) Pair AI/Tool
- 将 `AIMessage(thinking+action)` 与后续 `ToolMessage(result)` 配对为一个 `ActionNode`；
- 若缺失 ToolMessage，节点标记 `pending_output`；
- 产出标准化节点序列供后续建图。

### (4) Graph Build（raw + clean）
- 基于配对后的节点构建 Raw Graph（保留失败/重试/分支）；
- 从 Raw Graph 派生 Clean Graph（移除失败噪音，仅保留有效完成路径）；
- 依据 02 文档规则抽取 `dataflow/controlflow/temporal` 边；
- 若发现环，保留环信息并标注 `graph_has_cycle=true`（不阻断入库）。

### (5) Summarize
- 仅生成 **trajectory-level** L0/L1（当前不生成 node-level L0/L1）；
- 失败轨迹额外生成 `failure_signature`（如 no such function/column/syntax）。

### (6) Persist
- Raw/Clean 节点与边写入 Graph Store（如 Neo4j）；
- 文件系统写入 `ctx://agent/{agent_id}/memories/trajectories/{trajectory_id}/`：
  - `trajectory.json`
  - `graph_pointer.json`
  - `.abstract.md`
  - `.overview.md`
  - `raw_steps.jsonl`（可选，保留回放原文）
- 将 `Trajectory-L0/L1` 写入向量索引（不依赖 node-level 摘要）；
- 标量索引字段：`tenant_id, agent_id, task_type, tool_set, lifecycle_status, stale_flag, created_at`。

### (6.1) Semantic Indexer 实现要求（参考 OpenViking）

目标：在“类文件系统存储 + 独立向量索引”模式下，实现稳定的 RAG 索引写入。

实现约束：
- 仅索引 trajectory-level 两个文档：
  - L0: `.../.abstract.md`
  - L1: `.../.overview.md`
- 不索引 node-level 摘要（当前阶段）。

推荐流水线（异步）：
1. commit 完成后发布 `TrajectoryCommitted` 事件；
2. Indexer 从 FS 读取 `.abstract.md/.overview.md`；
3. 构造两条 `IndexDoc` 并入 `EmbeddingQueue`；
4. Embed worker 产出向量并执行 `upsert` 到 Vector Store。

`IndexDoc` 最小字段建议：
```python
class IndexDoc:
    id: str                 # 向量记录主键（md5(tenant_id + seed_uri)）
    uri: str                # 文本入口 URI（.../.abstract.md 或 .../.overview.md）
    parent_uri: str         # 父目录 URI（范围过滤辅助字段）
    level: int              # 层级：0=L0，1=L1
    tenant_id: str          # 租户隔离字段
    owner_space: str        # 可见性作用域（ACL 过滤字段）
    trajectory_id: str      # 所属轨迹 ID（回源关联）
    agent_id: str           # 轨迹所属 agent ID
    task_type: str | None   # 任务类型标签（检索过滤/聚类分桶）
    lifecycle_status: str   # 生命周期状态（active/cold/archived/deleted）
    stale_flag: bool        # 兼容性标记（true 表示被变更传播标记为 stale）
    created_at: datetime    # 首次写入时间
    updated_at: datetime    # 最近更新时间
```

Embedding Worker 根据 `uri` 回文件系统读取对应的 `.abstract.md/.overview.md` 作为向量化文本。

幂等策略：
- 使用确定性 ID + upsert（不 insert）；
- `seed_uri` 规则与 OpenViking 一致：L0/L1 对应固定后缀 URI；
- 同一 trajectory 的重复 commit 只会覆盖更新，不会产生脏重复。

### (7) Register deps & events
- 依赖写入 `.deps.json`（依赖的 skill/table/other trajectory）；
- 同步维护“反向依赖索引”（`dep_uri -> trajectory_ids`）以支持变更快速命中；
- 发出 `TrajectoryCommitted` 事件，供反馈与传播模块消费。

## 3.4 增量 commit（partial trajectory）

支持上层在长任务中间多次提交：

- `is_incremental=true` + `trajectory_id`；
- 新增节点 append，raw/clean 边做局部重建；
- 版本号自增：`graph_version = vN+1`（raw/clean 同步演进）；
- 检索默认优先最新版本，保留历史可回放。

## 3.5 异常与降级策略

- **解析失败**：保留 raw 节点 + `parse_error` 标记，不中断整条轨迹；
- **图构建低置信**：回退 temporal edges，供后续离线修复；
- **图后端写失败**：文件系统不落最终 pointer，写入重试队列并返回 `accepted_with_retry`；
- **向量写失败**：图后端 + 文件系统先落盘，向量异步重试；
- **重复提交**：返回已有 `trajectory_id` 与 `dedup=true`。

## 3.6 Commit 质量指标

- Graph Build Success Rate
- Avg Edge Confidence
- Parse Error Rate
- Incremental Merge Success Rate
- Commit P50/P99 Latency

