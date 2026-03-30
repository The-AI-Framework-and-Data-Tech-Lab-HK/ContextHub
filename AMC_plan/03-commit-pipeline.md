# 03 — Commit 设计：轨迹入库与图构建

## 3.1 API 草案

```python
POST /api/v1/amc/commit

request = {
  "account_id": "...",
  "agent_id": "...",              # 兼容字段；推荐从 X-Agent-Id 读取
  "scope": "agent",
  "owner_space": "agent-a",
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
- account_id / agent_id / scope / owner_space 一致性校验；
- 幂等键检查（`account_id + task_id + hash(trajectory)`）。

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
- 依据 02 文档规则抽取 `dataflow/reasoning/retry/temporal` 边；
- `dep_type` 必须显式区分：`dataflow`（真实 output->input）/ `reasoning`（thinking 参考执行结果）/ `retry`（失败后的同工具重试）/ `temporal`（时序兜底）；
- 若发现环，保留环信息并标注 `graph_has_cycle=true`（不阻断入库）。

#### (4.1) Dataflow 提取策略（实现草案）

目标：让 `A -> B` 的 `dataflow` 边表示“`A.tool_output` 的某个信息被 `B.tool_args` 消费”。

输入（按节点时间顺序）：
- `node.tool_args: dict | None`（结构化）
- `node.tool_output: dict | None`（结构化，至少包含 `status/data/error/...`）

核心思路：
1. 对每个节点构建两类“可匹配信号”：
   - `output_signals(A)`：从 `A.tool_output` 抽取候选 token（如表名、列名、文件路径、SQL 片段、错误码、主键值）；
   - `input_signals(B)`：从 `B.tool_args` 抽取候选 token（同类）。
2. 仅在 `A.ai_step < B.ai_step` 的前提下比较；
3. 若 `overlap(output_signals(A), input_signals(B))` 为空，则不判定 dataflow；
4. 若有重叠，按证据类型打分并形成 `dataflow`：
   - 强证据（权重高）：完整路径、表名+列名组合、明确 ID/键值复用；
   - 中证据：SQL 片段/错误消息关键词复用；
   - 弱证据：泛化词重叠（需阈值抑制）。
5. 若 `B` 无任何 dataflow 命中，则可按配置补一条 `temporal` 兜底边（通常 `prev(B) -> B`，低置信度）。

建议伪代码：

```python
def build_dataflow_edges(nodes, temporal_fallback: bool = True):
    edges = []
    for j, dst in enumerate(nodes):
        if not dst.tool_args:
            continue

        in_sig = extract_input_signals(dst.tool_args)
        best_hits = []
        for i in range(j):
            src = nodes[i]
            if not src.tool_output:
                continue
            out_sig = extract_output_signals(src.tool_output)
            hit = match_signals(out_sig, in_sig)  # -> {tokens, score, evidence_type} | None
            if hit and hit["score"] >= DATAFLOW_THRESHOLD:
                best_hits.append((src, hit))

        if best_hits:
            for src, hit in top_k_hits(best_hits, k=2):
                edges.append(
                    Edge(
                        src=src.node_id,
                        dst=dst.node_id,
                        dep_type="dataflow",
                        signal=hit["evidence_type"],  # e.g. "table+column", "filepath", "sql_fragment"
                        confidence=hit["score"],
                    )
                )
        elif temporal_fallback and j > 0:
            prev = nodes[j - 1]
            edges.append(
                Edge(
                    src=prev.node_id,
                    dst=dst.node_id,
                    dep_type="temporal",
                    signal=None,
                    confidence=0.2,
                )
            )
    return edges
```

`extract_*_signals` 推荐规则（MVP）：
- 路径类：`/a/b/c`、`*.csv`、`*.json`、`*.sqlite`
- SQL 类：`FROM/JOIN table`、`SELECT col`、`WHERE col=...`
- 结构化类：`columns[]`、`table_name`、`db_path`、`error.code`
- 值类：高信息密度 token（长度阈值 + 停用词过滤）

说明：以上是 **MVP 优先规则**，不是“仅允许这些类型命中”的白名单。

对其他数据类型，采用三层策略：
1. 强规则层（高权重）：路径/表列/主键/错误码等可解释结构；
2. 通用规则层（中低权重）：任意 `dict/list/scalar` 规范化后做 token 匹配（含 key-path）；
3. 兜底层：当强+通用规则都无法达到阈值时，不判定 dataflow，按配置可回退 temporal。

通用规则建议：
- `dict`：展开为 `key_path -> value_token`（如 `result.rows[0].id=...`）；
- `list`：元素去重后转 token 集合并计算交集比例；
- 数值：按类型保留（int/float/date），仅在同字段语义下做弱匹配；
- 长文本：分词后去停用词，仅保留高信息密度 token。

#### (4.2) 关键场景：枚举输出 -> 下一步命令字符串

场景（当前样例中常见）：
- 节点 A 的 `tool_output.data` 返回候选对象列表（如表名集合）；
- 节点 B 的 `tool_args.command` 使用其中某个值（如 `PRAGMA table_info(ch___company_info);`）。

该场景应判定为 `dataflow`，不应退化为 `temporal`。

建议实现：
1. 在 `extract_output_signals` 中，把 `tool_output.data[*]` 的字符串值抽成 `enum_output_tokens`；
2. 在 `extract_input_signals` 中，把 `tool_args.command` 抽成 `command_tokens`（保留下划线与数字）；
3. 匹配时增加规则：
   - 若 `enum_output_tokens ∩ command_tokens` 非空，记为强/中强证据（`evidence_type="enum_to_command"`）；
   - 置信度建议 `0.68~0.85`（随命中 token 数和唯一性增减）。
4. 去重约束：
   - 先做“输出减输入回显”（避免 `file_path -> db_path` 这类 echo 被误判）；
   - 再做 `enum_to_command` 命中判定。

建议伪代码（追加到 `match_signals` 内）：

```python
enum_hit = out_sig.enum_output_tokens & in_sig.command_tokens
if enum_hit:
    score = 0.68 + 0.06 * min(len(enum_hit), 3)
    score = min(score, 0.85)
    return {
        "tokens": enum_hit,
        "score": score,
        "evidence_type": "enum_to_command",
    }
```

可解释性要求：
- `signal_detail.matched_tokens` 记录具体命中值（如 `["ch___company_info"]`）；
- `signal_detail.evidence_type="enum_to_command"`；
- 可视化时允许将命中 token 标在 dataflow 边上。

质量控制：
- 避免“一词命中即连边”（最低 token 数与组合证据阈值）；
- `signal` 必须记录命中证据类型，便于调试；
- `dataflow` 与 `temporal` 置信度分布要明显分层（例如 >0.6 vs <0.3）。

#### (4.3) Reasoning 边提取策略（LLM）

目标：让 `A -> B` 的 `reasoning` 边表示“`B.thinking` 是基于 `A` 的执行结果形成的”。

建议输入（按时间顺序）：
- `node.thinking`
- `node.tool_output`
- `node.effective_tool_output`（去除输入回显后）
- `node.tool_args`（仅作为 dst 侧上下文，不可作为 src 输出证据）

当前实现由 LLM **两次独立调用** 抽取（先 dataflow、再 reasoning），以提升稳定性并便于排查：

```json
{
  "dataflow_edges": [...],
  "reasoning_edges": [
    {
      "src_node_id": "...",
      "dst_node_id": "...",
      "confidence": 0.0,
      "reason_summary": "...",
      "matched_evidence": ["..."]
    }
  ]
}
```

抽取约束（必须）：
1. `src.ai_step < dst.ai_step`；
2. `reasoning` 证据优先来自 `src.effective_tool_output` 与 `dst.thinking` 的对应；
3. **禁止**把 `src.tool_args` 当作 source 证据；
4. 若无法给出简短可解释原因，不建 `reasoning` 边。

后处理校验（建议）：
- 保留时序约束：`src.ai_step < dst.ai_step`；
- `reason_summary` 不能为空；
- `matched_evidence` 优先给可回溯短语，但不强制要求逐字命中（允许语义归纳）。

`reasoning.confidence` 计算建议（MVP）：
- 由 LLM 先给出 `base_confidence`（0~1）；
- 再由后处理做证据校正（示例）：
  - `+0.10`：`matched_evidence` 命中 >= 2 个独立证据；
  - `+0.05`：`matched_evidence` 同时覆盖结构字段与值（如 `columns` + 具体列名）；
  - `-0.10`：证据仅为高频泛词（如 `status/success/data`）；
  - `-0.15`：`dst.thinking` 仅弱提及（无明确引用语义）。
- 最终 `confidence = clamp(base + adjust, 0, 1)`。

`reasoning_min_confidence=0.55` 的设置理由：
- 低于 `0.5` 时，LLM 容易产出“语义相关但证据不足”的边（噪声明显增加）；
- 设为 `0.55` 能保留多数可解释推理边，同时抑制弱相关边；
- 后续可通过离线评估再调参（建议在 `0.50~0.65` 区间网格搜索）。

关于 `reasoning` 与 `dataflow` 的关系：
- 两者可以并存：`dataflow` 说明参数消费，`reasoning` 说明思考依据；
- 检索与可视化要用不同颜色区分，避免语义混淆。

### (5) Summarize
- 仅生成 **trajectory-level** L0/L1（当前不生成 node-level L0/L1）；
- 优先使用 LLM 生成：
  - L0（`.abstract.md`）：100-150 字，覆盖任务目标、关键步骤、执行效果；
  - L1（`.overview.md`）：600-800 字，覆盖主要路径、阶段动作、关键输出、失败/重试与最终效果；
- 若未配置可用 LLM 凭据或调用失败，自动回退到 rule-based 摘要，保证 commit 可用性；
- 失败轨迹可额外生成 `failure_signature`（如 no such function/column/syntax）。

### (6) Persist
- Raw/Clean 节点与边写入 Graph Store（如 Neo4j）；
- 文件系统写入 `ctx://agent/{agent_id}/memories/trajectories/{trajectory_id}/`：
  - `trajectory.json`
  - `graph_pointer.json`
  - `.abstract.md`
  - `.overview.md`
  - `llm_extraction/`（可选；按调用落盘，如 `01_dataflow.json`、`02_reasoning.json`、`03_summary.json`）
  - `raw_steps.jsonl`（可选，保留回放原文）
- 将 `Trajectory-L0/L1` 写入向量索引（不依赖 node-level 摘要）；
- 标量索引字段：`account_id, scope, owner_space, agent_id, task_type, tool_set, status, stale_flag, created_at`。

### (6.1) Semantic Indexer 实现要求（参考 OpenViking）

目标：在“类文件系统存储 + 独立向量索引”模式下，实现稳定的 RAG 索引写入。

实现约束：
- 仅索引 trajectory-level 两个文档：
  - L0: `.../.abstract.md`
  - L1: `.../.overview.md`
- 向量库记录以 `uri` 为主键锚点（`id = md5(account_id + uri)`），**不直接持久化文件全文**；
- Embedding 计算时由 worker 按 `uri` 回文件系统读取文本（运行时读取，向量库仅存 embedding + metadata）；
- **Embedding 输入必须是目标文件原文全文**（按文件编码解码后的完整字符串），禁止改用摘要字段副本、清洗重写文本、截断文本；
- metadata 至少包含：`uri/level/account_id/scope/owner_space/trajectory_id/agent_id/task_type/status`（可选 `content_sha256` 用于变更检测）；
- 不索引 node-level 摘要（当前阶段）。

推荐流水线（异步）：
1. commit 完成后发布 `TrajectoryCommitted` 事件；
2. Indexer 从 FS 读取 `.abstract.md/.overview.md`；
3. 构造两条 `IndexDoc`（以 `uri` 为锚，不内嵌正文）并入 `EmbeddingQueue`；
4. Embed worker 按 `uri` 回 FS 读取文件原文正文，产出向量并执行 `upsert` 到 Vector Store。

`IndexDoc` 最小字段建议：
```python
class IndexDoc:
    id: str                 # 向量记录主键（md5(account_id + seed_uri)）
    uri: str                # 文本入口 URI（.../.abstract.md 或 .../.overview.md）
    parent_uri: str         # 父目录 URI（范围过滤辅助字段）
    level: int              # 层级：0=L0，1=L1
    account_id: str         # 账户隔离字段
    scope: str              # 作用域（agent/team/datalake/user）
    owner_space: str        # 可见性作用域（ACL 过滤字段）
    trajectory_id: str      # 所属轨迹 ID（回源关联）
    agent_id: str           # 轨迹所属 agent ID
    task_type: str | None   # 任务类型标签（检索过滤/聚类分桶）
    lifecycle_status: str   # 生命周期状态（active/cold/archived/deleted）
    stale_flag: bool        # 兼容性标记（true 表示被变更传播标记为 stale）
    created_at: datetime    # 首次写入时间
    updated_at: datetime    # 最近更新时间
```

Embedding Worker 根据 `uri` 回文件系统读取对应的 `.abstract.md/.overview.md` 原文全文作为向量化文本。

Vector Store 推荐记录结构（MVP）：
- `id`：`md5(account_id + uri)`
- `embedding`：向量
- `metadata`：`uri, parent_uri, level, account_id, scope, owner_space, trajectory_id, agent_id, task_type, status, lifecycle_status, stale_flag, updated_at, content_sha256`
- 不存 `document/content` 原文字段（正文仅在 FS）

幂等策略：
- 使用确定性 ID + upsert（不 insert）；
- `seed_uri` 规则与 OpenViking 一致：L0/L1 对应固定后缀 URI；
- 重复 commit 时按 `uri` 读取文件原文并计算 `content_sha256`：
  - 若 hash 未变：可跳过重算 embedding（仅更新时间戳可选）；
  - 若 hash 变化：必须重算 embedding 并 upsert 覆盖旧向量；
- 同一 trajectory 的重复 commit 不产生脏重复。

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
- **依赖识别不足**：回退 temporal edges，并记录 `unresolved_dependency_signals` 供后续离线修复；
- **图后端写失败**：文件系统不落最终 pointer，写入重试队列并返回 `accepted_with_retry`；
- **向量写失败**：图后端 + 文件系统先落盘，向量异步重试；
- **重复提交**：返回已有 `trajectory_id` 与 `dedup=true`。

## 3.6 Commit 质量指标

- Graph Build Success Rate
- Avg Edge Confidence
- Parse Error Rate
- Incremental Merge Success Rate
- Commit P50/P99 Latency

## 3.7 与 main memory/search 的分层对齐方案（存储结构 + commit 入参）

目标：让 AMC 的 commit 语义与 main 分层模型保持一致，采用：
- `account_id`（租户隔离，来自请求头）
- `scope`（`agent|team|datalake`）
- `owner_space`（`agent_id` 或 `team path`）

### (1) 文件存储结构对齐（LocalFS）

当前 AMC 路径以 `ctx://agent/{agent_id}/...` 为主。对齐后采用三层目录语义：

```text
data/content/
  accounts/{account_id}/
    scope/{scope}/{owner_space}/
      memories/trajectories/{trajectory_id}/...
```

说明：
- `team_path` 与 main 的 `owner_space` 语义一致（可为空字符串表示根 team）；
- 目录只是落盘表达，权限判定仍由 API + ACL 决定；
- `graph_pointer.json` 增补 `scope`、`owner_space`、`account_id` 字段。

### (2) URI 规则对齐

commit 生成的 trajectory URI 对齐 main 规范：
- `scope=agent`：`ctx://agent/{owner_space}/memories/trajectories/{trajectory_id}`
- `scope=team`：`ctx://team/{owner_space}/memories/trajectories/{trajectory_id}`
- `scope=datalake`：`ctx://datalake/memories/trajectories/{trajectory_id}`

并在 commit 侧做 URI/作用域一致性校验（与 main `ContextService._validate_uri_scope` 同口径）。

### (3) Commit API 改动（兼容版）

建议由“body 直传 tenant/agent”切换为“header 上下文 + body 作用域”：

请求头（新增）：
- `X-Account-Id`（必须）
- `X-Agent-Id`（必须）

请求体（新增）：
- `scope: "agent" | "team" | "datalake"`（默认 `agent`）
- `owner_space: str | null`（agent/team 必填规则同 main）

兼容策略：
1. 第一阶段保留 `tenant_id/agent_id` 字段，但标记 deprecated；
2. 若 header 缺失则回退旧字段；
3. 第二阶段移除 `tenant_id` body 入参，仅保留 header。

### (4) Commit 写入口径

每次 commit 落盘必须携带并持久化：
- `account_id`
- `scope`
- `owner_space`
- `agent_id`（actor）

并用于：
- 向量 metadata 过滤；
- Neo4j trajectory 节点属性；
- 审计日志维度。

### (4.1) 为 retrieve 标量过滤准备的 commit 元数据（必须）

为支持 pgvector 的“先标量过滤再相似度排序”，commit 必须在索引写入时稳定产出以下 metadata：

- `account_id`（强制，租户隔离主键）
- `scope`（`agent|team|datalake`）
- `owner_space`（agent_id 或 team path）
- `status`（至少支持 `active/stale/archived/deleted`）
- `task_type`
- `tool_set`（可选，建议保留用于约束过滤）
- `trajectory_id`
- `agent_id`
- `uri`

要求：
1. 以上字段由 commit 统一写入向量 metadata（L0/L1 两条记录都一致）；
2. 字段缺失时 commit 直接报错（或降级为 `accepted_with_retry`，不可静默落空）；
3. metadata 版本化（建议 `meta_schema_version`），便于迁移和回填。

若 commit 不写齐这些字段，retrieve 的标量过滤将失效或误召回。

### (5) Team 共享场景

对齐 main 的 memory promote 语义，AMC 增补“共享发布”模式：
- agent 轨迹默认写 `scope=agent, owner_space={agent_id}`；
- 发布到团队时写 `scope=team, owner_space={team_path}`；
- 可选建立 `derived_from` 关系（team 轨迹 <- agent 源轨迹）。

