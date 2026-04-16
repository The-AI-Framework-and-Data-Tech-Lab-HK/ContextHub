# 17 — AMC 批量 Commit 实现方案（Batch Trajectory Commit）

## 17.1 背景与目标

当前 `commit` 路径按“单条轨迹”处理：一次请求只处理一个 trajectory，并在同一链路内依次执行：

1. validate/normalize/pair/build graph/summarize；
2. 写文件系统（trajectory bundle）；
3. 写图后端（Neo4j）；
4. 写向量索引（L0/L1 embedding + upsert）。

当轨迹规模上来（例如离线回灌、批量导入历史任务）时，该模型存在吞吐瓶颈，主要体现在：
- LLM 调用次数与轨迹条数线性增长；
- FS/Graph/Vector 的写入开销重复支付；
- 端到端延迟由“单条串行”放大。

本方案目标：
- 支持一次提交多条 trajectory；
- 支持“批量输入 -> LLM 批处理 -> 批量持久化”主路径；
- 保持现有单条 commit 兼容不破坏；
- 保证可观测、可回滚、可降级。

非目标（本期不做）：
- 全局强事务（跨 FS + Neo4j + pgvector 的 ACID 一致性）；
- 改写 retrieve 语义；
- 变更 propagation/feedback 模块逻辑。

---

## 17.2 新接口草案

建议新增：

`POST /api/v1/amc/commit/batch`

请求头：
- `X-Account-Id`（必填）
- `X-Agent-Id`（必填）

请求体（建议）：

```json
{
  "batch_id": "batch_20260416_001",
  "scope": "agent",
  "owner_space": "agent-a",
  "options": {
    "fail_fast": false,
    "llm_batch_size_hint": 8,
    "llm_max_items_per_batch": 16,
    "llm_token_usage_ratio": 0.6,
    "persist_batch_size": 32
  },
  "items": [
    {
      "session_id": "s1",
      "task_id": "t1",
      "trajectory_id": null,
      "trajectory": [],
      "labels": {},
      "is_incremental": false
    },
    {
      "session_id": "s2",
      "task_id": "t2",
      "trajectory_id": null,
      "trajectory": [],
      "labels": {},
      "is_incremental": false
    }
  ]
}
```

请求字段补充说明：
- `labels`：透传业务标签（例如业务线、任务类别、来源系统、离线导入批次号），会落到 trajectory 元数据，供后续过滤、审计与分析使用。
- `is_incremental`：是否把该条作为“已有 trajectory 的增量片段”处理。`true` 时要求配合既有 `trajectory_id`，沿用增量合并语义；`false` 表示完整提交。
- `llm_batch_size_hint`：初始批大小建议值（默认 8），仅作为打包起点，不是硬上限。
- `llm_max_items_per_batch`：单个 LLM 微批的 item 上限（建议 16），避免一次请求承载过多轨迹。
- `llm_token_usage_ratio`：微批 token 预算比例（建议 0.6），即最多使用 provider 可用上下文窗口的 60%。

返回体（建议）：

```json
{
  "batch_id": "batch_20260416_001",
  "status": "accepted_partial",
  "summary": {
    "total": 100,
    "accepted": 95,
    "idempotent": 3,
    "failed": 2
  },
  "items": [
    {
      "item_id": "0",
      "trajectory_id": "traj_xxx",
      "status": "accepted",
      "warnings": [],
      "idempotency_key": "..."
    },
    {
      "item_id": "1",
      "trajectory_id": null,
      "status": "failed",
      "error_code": "VALIDATION_ERROR",
      "error_message": "..."
    }
  ]
}
```

语义约定：
- `status` 采用 item 级语义，不因单条失败导致整批回滚；
- `accepted_partial` 表示批处理中存在失败项；
- 失败项可重试，不影响已成功项。

---

## 17.3 批处理总体流程

```text
Receive Batch
  -> Validate batch envelope
  -> Per-item pre-validate + normalize
  -> Build LLM micro-batches (token-aware packing)
  -> Batch LLM extract/summarize
  -> Build per-item raw/clean graph
  -> Batch persist:
       FS batch write -> Graph batch write -> Vector batch upsert
  -> Audit + per-item result aggregation
```

### 17.3.1 关键原则

1. **先算后写**：先完成所有 item 的计算产物（graph + summary），再进入批量持久化；
2. **微批执行**：一个大 batch 自动拆分为多个 micro-batch（受 token、请求体、超时限制）；
3. **可降级**：批量 LLM 失败时可降级到单条 LLM，不阻断整批；
4. **幂等优先**：保留现有 per-item idempotency 语义，避免重复写放大；
5. **部分成功可接受**：默认 item 级提交，不做全批强事务。

---

## 17.4 LLM 批处理设计

用户目标要求“多条轨迹作为一个 batch 交给大模型处理”，建议按“逻辑批 + 微批”实现。

### 17.4.1 输入打包

每个 item 先完成本地步骤：
- `validate_raw_steps`
- `truncate_tool_output`
- `pair_ai_tool_steps`

然后构造 LLM 输入单元：
- `item_id`
- `trajectory_id`（若未传则先生成 deterministic id）
- `pairs`（供 dataflow/reasoning）
- `normalized_steps`（供 summary）

再按 token 估算进行微批打包（动态）：
- `llm_batch_size_hint` 默认 8（仅 hint）；
- 从 provider 能力获取 `max_context_tokens`（失败时回退本地配置）；
- 可用预算：`token_budget = floor(max_context_tokens * llm_token_usage_ratio)`，默认 ratio=0.6；
- 打包停止条件：达到 `token_budget` 或达到 `llm_max_items_per_batch`（建议 16）。

建议打包算法：
1. 读取 provider `max_context_tokens`（优先模型元数据/能力接口）；
2. 计算 `token_budget`（例如 200k * 0.6 = 120k）；
3. 按 item 估算 token（含系统提示与结构开销）并顺序装箱；
4. 每个微批最多 16 条（即使 token 仍有余量）；
5. 若单条 item 已超过预算，单独成批并走降级（截断/规则模式/失败返回）。

### 17.4.2 Dataflow/Reasoning 批提取

新增批接口（建议）：
- `LLMDataflowExtractor.extract_batch(items: list[...]) -> dict[item_id, edges]`

输出要求：
- 每个 item 独立返回 `dataflow_edges/reasoning_edges`；
- 保留 `last_traces` 的 batch 版本，trace 包含 `batch_id/item_id`。

降级策略：
- 若某个 item 在 batch 结果解析失败，仅该 item 回退 rule-based 或单条 LLM；
- 若整个 batch 调用失败，批次内 item 可切换单条 LLM（受 `fail_fast` 控制）。

### 17.4.3 Summary 批生成

新增批接口（建议）：
- `LLMTrajectorySummarizer.summarize_batch(items) -> dict[item_id, (l0, l1)]`

降级策略：
- 批量失败：回退单条 LLM；
- 单条 LLM 失败：回退 rule-based summarizer。

### 17.4.4 当前实现状态（2026-04-16）

- 已落地“token-aware 微批”到 `/commit/batch` 的 prepare 阶段：
  - 先估算每条轨迹的 LLM token 负载；
  - 用 `llm_token_usage_ratio * max_context_tokens` 作为预算；
  - 同时受 `llm_max_items_per_batch` 限制；
  - 每个微批内部再做并行 prepare（`ThreadPoolExecutor`）。
- `max_context_tokens` 获取策略：
  - 优先从 provider 模型元数据读取；
  - 失败时回退 `llm_max_context_tokens_fallback`（默认 24000）。
- 已落地 provider 级限流/重试：
  - provider+model 维度并发门控（semaphore）；
  - 429/5xx/连接超时等可重试错误指数退避 + jitter；
  - 支持读取 `Retry-After` 头；
  - trace 追加 `retry_count/prompt_tokens/completion_tokens/total_tokens`。

---

## 17.5 批量持久化设计

### 17.5.1 文件系统（LocalFS）批写

现状：`save_bundle(...)` 每条调用都会读写 `_index.json/_uri_index.json/_idempotency.json`。  
优化方向：新增 `save_bundles_batch(...)`，降低索引文件反复读写。

建议实现：
1. 逐 item 写独立目录文件（`trajectory.json/raw_graph.json/clean_graph.json/...`）；
2. 内存汇总 index/uri/idempotency 的增量；
3. 一次性 flush 三个索引文件。

收益：
- 降低 JSON 索引文件读写次数；
- 保持目录级资产隔离，不改变 replay 读路径。

### 17.5.2 Graph Store（Neo4j）批写

现状：`upsert_trajectory_graphs(...)` 一次仅一条，内部多次 `execute_write`。  
优化方向：新增 `upsert_trajectory_graphs_batch(...)`。

建议实现：
- 按 `persist_batch_size` 切分；
- 每个 micro-batch 用单事务 + `UNWIND` 写节点/边；
- 保持每条 trajectory 的 `clear + merge` 语义不变。

注意点：
- Neo4j 单事务 payload 过大可能触发超时，必须切批；
- 失败时返回失败的 trajectory_id 列表，供 item 级重试。

### 17.5.3 Vector Store 批写（embedding + upsert）

现状：`TrajectoryVectorIndexer.index_trajectory(...)` 按条处理，最多 L0/L1 两文档。  
优化方向：新增 `index_trajectories_batch(...)`。

建议实现：
1. 汇总全部待索引文档（L0/L1）；
2. 先做 `content_sha256` 去重检查，跳过未变化文档；
3. embedding 请求按 batch 发送（受模型与接口上限控制）；
4. `vector_store.upsert_embeddings(records)` 已支持批 records，可直接复用。

---

## 17.6 一致性与失败语义

跨 FS/Neo4j/pgvector 不做分布式事务，采用“可恢复最终一致”策略。

### 17.6.1 item 状态机（建议）

- `computed`：计算产物完成（graph/summary ready）
- `fs_persisted`
- `graph_persisted`
- `vector_persisted`
- `accepted`（全部完成）
- `failed_*`（在某阶段失败）

每个 item 返回失败阶段与错误码，便于补偿重试。

### 17.6.2 写入顺序与补偿

建议顺序：`FS -> Graph -> Vector` 或保持当前 `Graph -> FS -> Vector`。  
考虑到 replay 依赖 FS，建议批路径调整为 `FS -> Graph -> Vector`，并在文档明确：
- 若 Graph 失败：item 标记 `accepted_with_retry` 或 `failed_graph`，进入重试队列；
- 若 Vector 失败：不影响 replay/retrieve 图召回，标记 `failed_vector` 并异步重试。

### 17.6.3 幂等语义

保留现有 per-item 幂等键：
- `idempotency_key = hash(account_id + task_id + normalized_trajectory)`

新增 batch 幂等（可选）：
- `batch_fingerprint = hash(account_id + owner_space + sorted(item_idempotency_keys))`
- 用于识别“同一批次重复提交”并快速返回结果摘要。

---

## 17.7 配置项扩展建议（12-config 对齐）

建议新增：

```yaml
commit:
  batch:
    enabled: true
    max_items_per_request: 200
    llm_batch_size_hint: 8
    llm_max_items_per_batch: 16
    llm_token_usage_ratio: 0.6
    llm_max_context_tokens_fallback: 24000
    persist_batch_size: 32
    fail_fast: false
    allow_partial_success: true
```

环境变量覆盖（示例）：
- `AMC_COMMIT_BATCH_ENABLED`
- `AMC_COMMIT_BATCH_MAX_ITEMS_PER_REQUEST`
- `AMC_COMMIT_BATCH_LLM_BATCH_SIZE_HINT`
- `AMC_COMMIT_BATCH_LLM_MAX_ITEMS_PER_BATCH`
- `AMC_COMMIT_BATCH_LLM_TOKEN_USAGE_RATIO`
- `AMC_COMMIT_BATCH_LLM_MAX_CONTEXT_TOKENS_FALLBACK`
- `AMC_COMMIT_BATCH_PERSIST_BATCH_SIZE`
- `AMC_LLM_MAX_CONCURRENCY`
- `AMC_LLM_MAX_RETRIES`
- `AMC_LLM_BACKOFF_BASE_SECONDS`
- `AMC_LLM_BACKOFF_MAX_SECONDS`

---

## 17.8 代码改造清单（建议）

### API / Schema
- `src/api/schemas/commit.py`：新增 `BatchCommitRequest/BatchCommitResponse`（或新文件）
- `src/api/routes/commit.py`：新增 `POST /commit/batch`

### Orchestrator / Core
- `src/app/orchestrators/commit_orchestrator.py`：新增 `commit_batch(...)`
- `src/core/commit/service.py`：新增 `run_batch(...)`
- `src/core/commit/dataflow_llm.py`：新增 `extract_batch(...)`
- `src/core/commit/summary_llm.py`：新增 `summarize_batch(...)`

### Storage / Indexing
- `src/infra/storage/fs/trajectory_repo.py`：新增 `save_bundles_batch(...)`
- `src/infra/storage/graph/base.py`：扩展 batch 协议
- `src/infra/storage/graph/neo4j_adapter.py`：新增 `upsert_trajectory_graphs_batch(...)`
- `src/core/indexing/trajectory_vector_indexer.py`：新增 `index_trajectories_batch(...)`

### Test / Script
- `src/tests/unit/...`：batch service 与 batch adapter 覆盖
- `src/tests/integration/...`：批量接口、部分失败、幂等重提、性能基线
- `scripts/test_commit_api.py`：可选新增 `--batch-file` 或新增 `test_commit_batch_api.py`

---

## 17.9 分阶段实施建议

### Phase A（快速可用）
- 新增 `/commit/batch`；
- 先做“服务内并发批处理”（仍可先复用单条 LLM 调用）；
- 打通 item 级返回与失败语义。

### Phase B（完整批处理）
- 已完成：prepare 阶段并行（LLM 相关计算并发）；
- 已完成：provider 级限流 + 429 backoff 重试；
- 已完成：`llm_token_usage_ratio` 驱动的 token-aware 微批打包；
- 待继续：真正的单请求 LLM `extract_batch/summarize_batch` 接口化；
- 待继续：FS/Graph/Vector 原生批写接口化；
- 待继续：持久化阶段补偿状态持久化。

### Phase C（性能与稳定性）
- 估算器持续校准（estimated vs actual token）；
- Graph `UNWIND` 与 embedding 批请求调优；
- 增加压测、告警、SLO 守护。

---

## 17.10 测试与验收口径

### 功能正确性
- 100 条轨迹批量提交，成功轨迹可 replay；
- retrieve 可命中新提交 trajectory（语义与图）；
- 单条失败不影响其他条落库；
- 重复提交同批次不产生重复脏数据。

### 性能目标（建议）
- 与逐条 commit 相比，吞吐提升 >= 2x（同硬件同数据集）；
- 平均每条轨迹处理耗时下降 >= 40%；
- P99 不高于单条模式的 1.2x（按 item 计）。

### 稳定性目标
- Graph/Vector 任一后端短时不可用时，系统可返回部分成功并可重试补齐；
- 批量接口错误可定位到 item 级（item_id + stage + error_code）。

---

## 17.11 风险与缓解

1. **LLM 上下文超限**
   - 缓解：token-aware 微批切分 + 超限自动降级单条。

2. **批写放大单次失败影响**
   - 缓解：persist micro-batch + item 级补偿重试。

3. **幂等与重复写复杂化**
   - 缓解：坚持 item 级幂等为主，batch 幂等为辅。

4. **观测盲区（只看到 batch 看不到 item）**
   - 缓解：审计与指标增加 `batch_id/item_id/stage/status` 维度。

---

## 17.12 待确认决策

1. 默认写入顺序是否从当前 `Graph -> FS -> Vector` 调整为 `FS -> Graph -> Vector`；
2. `/commit/batch` 是否允许 item 覆盖 `scope/owner_space`（建议本期不允许，统一 batch 级）；
3. 是否要求 batch 级幂等强保证（需要额外结果缓存）；
4. LLM 批调用失败时，默认是“自动降级单条”还是“直接失败返回”。

