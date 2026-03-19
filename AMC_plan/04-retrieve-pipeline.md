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
          -> Graph Recall (subgraph / motif, only when partial_trajectory exists)
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

说明：
- `failure clues` 例如报错关键词（`no such column`、`syntax error`）；
- `scope` 与 `owner_space` 联动，避免越权召回。

索引来源说明：
- 向量记录来自 `ctx://agent/{agent_id}/memories/trajectories/{trajectory_id}/.abstract.md` 与 `.overview.md`；
- 向量库仅做“召回候选”，内容与证据由 FS/Graph 回源补齐。

## 4.4 轨迹相似召回（Graph Similarity）

若传入 partial trajectory：

1. 构建 query graph（QG）；
2. 提取结构特征：
   - 工具序列 n-gram
   - 失败->修复 motif
   - 分支宽度与深度
   - 关键依赖链长度
3. 在历史图上做近似匹配，得分 `graph_score`。

默认策略：
- 主召回使用 **Clean Graph**（减少失败噪音）；
- 当 query 明确包含报错线索时，补充 **Raw Graph** 的失败分支相似度。

无 partial trajectory 时，不执行图召回，直接走纯语义召回。

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

