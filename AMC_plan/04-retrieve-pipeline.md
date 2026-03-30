# 04 — Retrieve 设计：语义召回 + 轨迹图召回融合

## 4.1 API 草案

```python
POST /api/v1/amc/retrieve

request = {
  "tenant_id": "...",
  "agent_id": "...",
  "query": {
    "task_description": "...",
    "partial_trajectory": [...],      # 可选
    "constraints": {"tool_whitelist": ["local_db_sql"]},
    "task_type": "sales_analysis"     # 可选
  },
  "top_k": 5
}
```

```python
response = {
  "items": [
    {
      "trajectory_id": "...",
      "score": 0.87,
      "semantic_score": 0.82,
      "graph_score": 0.91,          # 无 partial_trajectory 时可为 null
      "rationale": ["same tool chain", "similar failure-fix pattern"],
      "evidence": {
        "matched_nodes": ["n12", "n18"],
        "matched_subgraph": "..."
      }
    }
  ]
}
```

响应字段说明：
- `score`：最终排序分（用于返回排序，不是单一模型分数）。
- `semantic_score`：语义召回相关分数（来自向量检索+语义重排）。
- `graph_score`：图相似分；无 `partial_trajectory` 时为 `null`。
- `rationale`：可读解释（为什么命中）。
- `evidence.matched_nodes`：图后端返回的关键命中节点 ID。
- `evidence.matched_subgraph`：结构命中摘要（用于可解释展示/调试）。

## 4.2 Retrieve 流程

```
Query Parse -> Build Query Graph (optional)
          -> Semantic Recall (vector, always)
          -> Graph Recall (max common subgraph, only when partial_trajectory exists)
          -> Candidate Union (if graph branch exists)
          -> Hybrid Rerank
          -> ACL Filter + Mask
          -> Return with evidence
```

## 4.3 语义召回（RAG）

- 检索对象：Trajectory-L0/L1（trajectory-level）；
- query embedding = `task_description + key constraints + failure clues`；
- 标量过滤：tenant、scope、task_type、tool_set；
- 产出 top-N（如 50）候选轨迹。

pgvector 执行策略（实现建议）：
1. 先 `WHERE` 做标量过滤（至少 `account_id`，可选 `scope/owner_space/status/task_type/tool_set`）；
2. 再 `ORDER BY embedding <-> :query_vector` 做相似度排序；
3. `LIMIT top_n` 取候选；
4. 候选仍需经过 ACL `filter_visible` 兜底。

示例（伪 SQL）：

```sql
SELECT id, metadata, embedding <-> :query_vec AS distance
FROM amc_trajectory_index
WHERE metadata->>'account_id' = :account_id
  AND metadata->>'status' <> 'deleted'
  AND (:scopes_is_null OR metadata->>'scope' = ANY(:scopes))
  AND (:owner_spaces_is_null OR metadata->>'owner_space' = ANY(:owner_spaces))
ORDER BY embedding <-> :query_vec
LIMIT :top_n;
```

说明：
- `failure clues` 例如报错关键词（`no such column`、`syntax error`）；
- `scope` 与 `owner_space` 联动，避免越权召回。

索引来源说明：
- 向量记录来自 `ctx://agent/{agent_id}/memories/trajectories/{trajectory_id}/.abstract.md` 与 `.overview.md`；
- 向量库仅做“召回候选”，内容与证据由 FS/Graph 回源补齐。

## 4.4 轨迹相似召回（Graph Similarity）

若传入 partial trajectory：

1. 构建 query graph（QG）；
2. 在候选历史图上计算最大公共子图（MCS）；
3. 将 MCS 命中规模归一化为 `graph_score`。

默认策略：
- 主召回使用 **Clean Graph**（减少失败噪音）；
- 本期仅实现基于 MCS 的工具调用结构匹配，不引入额外特征项。

无 partial trajectory 时，不执行图召回，直接走纯语义召回。

### 4.4.1 Query Graph 构建（QG）

输入：`query.partial_trajectory`（与 `sample_traj` 同结构，通常是“当前进行中的前缀轨迹”）。

构建步骤（与 commit 侧保持一致，避免 schema 漂移）：

1. 复用现有 trajectory parser，将 partial trajectory 转成 `ActionNode[]` 与时序边；
2. 执行 clean 规则（压缩空转步骤、保留关键工具调用、保留失败/重试链）；
3. 产出 `query_raw_graph` 与 `query_clean_graph`（默认使用 clean）；
4. 同时提取 query graph 的签名特征（见 4.4.2）。

设计约束：
- QG 只在本次检索请求内临时存在，不写回 memory；
- 若 QG 构建失败，降级为纯语义召回，并在 evidence 中标记 `graph_recall_skipped_reason`；
- QG 节点/边类型、edge_type 命名必须与 Neo4j 存量图一致（含 `retry`、`reasoning`、`dataflow`）。

### 4.4.2 相似度定义（MVP：仅 MCS）

本期 graph similarity 只考虑“工具调用结构”：

1. 在 `query_clean_graph` 与 `candidate_clean_graph` 间计算最大公共子图（MCS）；
2. 节点匹配规则：`action`（函数名）相同即可命中；
3. 边匹配规则：`edge_type` 相同即可命中（如同为 `dataflow`）；
4. 不要求 `tool_args`、`tool_output`、节点其他属性或边其他属性相同。

记分建议（0~1）：

```python
graph_score = (matched_mcs_edges + matched_mcs_nodes) / max(
    1, query_graph_edges + query_graph_nodes
)
```

说明：
- 以边覆盖率为主分，天然约束结构一致性；
- 若 query 边过少（如 0 或 1），可回退到节点覆盖率辅助打分；
- 后续版本再扩展其他特征项（motif/属性分布等）。

### 4.4.3 候选检索策略（两阶段）

为控制时延与吞吐，图匹配分两阶段：

阶段 A：候选收缩（coarse filter）
- 优先使用语义 top-N（如 50）作为图匹配候选池；
- 可选增加硬过滤：`task_type`、`tool_whitelist`、`tenant_id/agent_id`；
- 若语义分支为空，可回退到同 tenant/agent 的最新 K 条轨迹。

阶段 B：精匹配（fine scoring）
- 对候选池逐条读取 Neo4j clean graph；
- 按 MCS 规则计算 `graph_score`；
- 输出图分 top-M（如 10）供后续融合排序。

### 4.4.4 Neo4j 检索与计算边界

MVP 计算边界建议：
- Neo4j 负责按 `trajectory_id` 回源 clean 图结构；
- 相似度计算在应用层执行（Python），先实现可控的 MCS 版本；
- 仅在后续性能瓶颈出现时，再考虑下推或特征索引。

这样可保持：
- 一致性：graph source of truth 始终在 Neo4j；
- 可调试：MCS 命中节点/边可直接在日志/CLI中输出；
- 可演进：后续可替换为 graph embedding / GNN reranker。

### 4.4.5 返回证据（Graph Evidence）

当执行图召回时，每条结果新增 evidence（可裁剪展示）：

```json
{
  "graph_evidence": {
    "mcs_matched_node_count": 9,
    "mcs_matched_edge_count": 8,
    "mcs_node_match_rule": "action_name_equal",
    "mcs_edge_match_rule": "edge_type_equal",
    "graph_score": 0.80
  }
}
```

要求：
- 返回可解释字段，不返回过大子图全文（避免 payload 膨胀）；
- CLI 可加开关显示完整匹配子图（默认仅摘要）。

### 4.4.6 失败降级与质量守护

降级策略：
- `partial_trajectory` 为空：跳过图召回；
- QG 构建失败：跳过图召回，保留语义结果；
- Neo4j 超时/不可用：记录审计 + 降级语义结果；
- 图分异常（NaN/空）：该候选 graph_score 置 0 并继续排序。

质量守护：
- 线上默认记录 `graph_score` 分项统计（P50/P95）；
- 对短前缀 query（steps <= 4）启用低置信标记，避免误导；
- 在评测集中单独统计 `Graph-Match Precision@K` 与 `Failure-Fix Hit Rate`。

## 4.5 融合排序策略（MVP）

```python
final_score = w_sem * semantic_score + w_graph * graph_score + w_fb * feedback_boost

default:
  if partial_trajectory:
      w_sem=0.45, w_graph=0.45, w_fb=0.10
  else:
      final_score = 0.90 * semantic_score + 0.10 * feedback_boost
      # graph_score = null
```

`feedback_boost` 来自历史 adopted/ignored/corrected 信号（见 07/06 文档）。

术语补充：
- `feedback_boost`：基于历史反馈的奖励/惩罚项，不替代语义或图分。
- `Candidate Union`：语义候选与图候选去重合并后的统一候选池。

## 4.6 返回内容粒度

返回不只“整条轨迹”，还应返回最有用片段：

- trajectory-level 摘要（L0/L1）；
- node-level 推荐片段（从图后端按 node_id 读取关键动作 + 参数模式）；
- failure-fix 片段（如果 query 包含错误上下文）；
- 每条结果的“为何命中”证据。

回源策略：
1. 先从向量库拿 `trajectory_id + uri + semantic_score`；
2. 再回文件系统读取 `.overview.md` 和 `trajectory.json`；
3. 如需动作级证据，再通过 `graph_pointer` 到图后端取节点/边；
4. 最终统一做 ACL + 脱敏后返回。

## 4.7 安全与可见性

- 检索前过滤：tenant/scope ACL；
- 检索后脱敏：按 field_masks 清洗参数与结果；
- 审计落库：记录 query、命中 URI、是否被上层 agent 采用。

## 4.8 Retrieve 评估指标

- Trajectory Recall@K
- Graph-Match Precision@K
- Failure-Fix Hit Rate
- Context Adoption Rate（被上层 agent 采纳比例）
- Retrieve P50/P99 Latency

## 4.9 与 main search 接口对齐方案（retrieve API + ACL）

目标：AMC retrieve 在请求上下文、过滤语义、可见性判定上对齐 main `/api/v1/search`。

### (1) 请求上下文改造

retrieve 使用 header 注入上下文（与 main 一致）：
- `X-Account-Id`
- `X-Agent-Id`

请求体聚焦检索语义：
- `query` / `partial_trajectory` / `top_k`
- `scope`（可选：`agent|team|datalake` 列表）
- `owner_space`（可选，细粒度限定）

兼容：
- 过渡期可接受 `tenant_id/agent_id` body 字段；
- 优先使用 header，body 仅作 fallback。

### (2) 检索过滤口径

与 main `retrieval_service` 保持一致：
1. 先做向量/图候选召回（向量分支先标量过滤再相似度排序）；
2. 再按 `scope/context_type/status` 过滤；
3. 最后执行 ACL `filter_visible`。

ACL 规则对齐：
- `agent`：仅 `owner_space == X-Agent-Id`
- `team`：`owner_space` 在可见 team path 闭包内
- `datalake`：默认可读

### (3) 返回结构对齐

retrieve 结果补齐与 main `SearchResult` 相同的隔离字段：
- `scope`
- `owner_space`
- `uri`

同时保留 AMC 专有字段：
- `semantic_score`
- `graph_match_score`
- `total_score`
- `evidence`

### (4) clean_graph 返回策略（与安全策略协同）

默认返回简略 `clean_graph`（已实现）：
- node: `node_id/thinking/tool_name/tool_args/tool_output`（字段截断）
- edge: `src/dst/dep_type`

并在 ACL 通过后才允许返回图内容。
当请求 `include_full_clean_graph=true` 时，仍需执行字段脱敏策略。

### (5) 审计字段对齐

retrieve 审计日志增加：
- `account_id`
- `scope_filter`
- `owner_space_filter`
- `acl_visible_count_before/after`

用于与 main search 审计口径统一对账。

