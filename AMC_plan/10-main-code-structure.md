# 10 — AMC 主代码结构草案（开发态）

本文档定义 AMC（Agent Memory Core）的主代码结构、核心模块边界和交互流程，目标是：

1. 给实现阶段提供统一目录和接口蓝图；
2. 明确 `commit / retrieve` 主链路与 `feedback / propagation` 的耦合点；
3. 以开发便利优先（Vector=pgvector，Graph=Neo4j），同时保证后续可替换。

---

## 10.1 设计目标与约束

### 目标
- 快速落地 AMC MVP（commit/retrieve 可用）；
- 保持与 `AMC_plan/02~09` 的数据模型和阶段计划一致；
- 为生产态替换存储后端预留抽象层。

### 约束
- 图结构必须落 Graph Store（开发态 Neo4j）；
- 语义召回走 Vector Store（开发态 pgvector）；
- 文件系统仅保存 trajectory-level 元信息与 L0/L1 文档；
- ACL、审计、stale/lifecycle、feedback 作为跨链路标准能力，不做旁路实现。

---

## 10.2 顶层目录建议

```text
src/
  api/
    routes/
      commit.py
      retrieve.py
      feedback.py
      promote.py
      replay.py
    schemas/
      commit.py
      retrieve.py
      feedback.py
    deps.py

  core/
    commit/
      validator.py
      normalizer.py
      pairing.py
      graph_builder.py
      clean_deriver.py
      summarizer.py
      deps_extractor.py
      service.py

    retrieve/
      query_parser.py
      query_graph_builder.py
      semantic_recall.py
      graph_recall.py
      candidate_union.py
      reranker.py
      evidence_builder.py
      service.py

    feedback/
      collector.py
      quality_updater.py
      service.py

    propagation/
      event_consumer.py
      stale_marker.py
      reverse_dep_resolver.py
      service.py

    workflow/
      cluster.py
      subpath_mining.py
      template_builder.py
      service.py

  domain/
    models/
      trajectory.py
      action_node.py
      dependency_edge.py
      feedback.py
      change_event.py
      workflow_template.py
    value_objects/
      ids.py
      scores.py
      enums.py

  infra/
    storage/
      fs/
        trajectory_repo.py
      vector/
        base.py
        pgvector_adapter.py
      graph/
        base.py
        neo4j_adapter.py
      event/
        base.py
        jsonl_event_log.py

    security/
      acl_engine.py
      mask_engine.py
      policy_repo.py

    audit/
      audit_logger.py

    queue/
      embedding_queue.py
      retry_queue.py

  app/
    orchestrators/
      commit_orchestrator.py
      retrieve_orchestrator.py
      feedback_orchestrator.py
      propagation_orchestrator.py
    config.py
    wiring.py

  tests/
    unit/
    integration/
    e2e/
```

说明：
- `core/` 放业务算法与流程，不依赖具体 DB SDK；
- `infra/` 放后端适配器（pgvector/Neo4j/JSONL）；
- `app/orchestrators/` 负责把 `core + infra + security + audit` 串起来；
- `api/` 仅负责协议层（HTTP 输入输出和错误映射）。

---

## 10.3 主要代码块与职责

### A. API Layer
- 输入校验、鉴权上下文注入、响应格式标准化；
- 不做业务决策，只调用 orchestrator。

关键端点：
- `POST /api/v1/amc/commit`
- `POST /api/v1/amc/retrieve`
- `POST /api/v1/amc/feedback`
- `POST /api/v1/amc/promote`
- `GET /api/v1/amc/replay/{trajectory_id}`

### B. Commit Block
- `validator`：schema/step monotonic/idempotency check；
- `normalizer`：Action 解析、输出裁剪、字段补齐；
- `pairing`：AI/Tool step 配对为 ActionNode；
- `graph_builder + clean_deriver`：构建 raw graph 并派生 clean graph；
- `summarizer`：生成 trajectory L0/L1；
- `deps_extractor`：写 `.deps.json` 并更新反向依赖。

### C. Retrieve Block
- `query_parser`：解析 task 描述、约束、failure clues；
- `semantic_recall`：向量召回 L0/L1 候选；
- `graph_recall`：有 partial trajectory 时做结构召回；
- `reranker`：融合 semantic/graph/feedback；
- `evidence_builder`：组装命中节点、子图、rationale。

### D. Feedback Block
- 接收 adopted/ignored/corrected/irrelevant；
- 更新 trajectory 质量分、节点/边可复用度；
- 反哺 retrieve 的 `feedback_boost`。

### E. Propagation Block
- 消费 `ChangeEvent`；
- 用 reverse deps 定位受影响 trajectory；
- MVP 仅执行 `mark_stale`（并写审计、更新索引过滤字段）。

### F. Security & Audit Block
- `acl_engine`：deny-override + scope 评估；
- `mask_engine`：field-path 级别脱敏；
- `audit_logger`：commit/retrieve/feedback/promote/replay 全链路审计。

### G. Storage Adapters
- `VectorStore`（dev: pgvector）：L0/L1 upsert/search；
- `GraphStore`（dev: Neo4j）：raw/clean graph 持久化与查询；
- `FS Repo`：trajectory-level 文件读写；
- `EventLog`（dev: JSONL）：append-only 事件落盘。

---

## 10.4 关键接口（建议）

### 1) VectorStore（抽象）
```python
class VectorStore(Protocol):
    async def upsert_docs(self, docs: list[IndexDoc]) -> None: ...
    async def search(self, query_vec: list[float], filters: dict, top_k: int) -> list[VectorHit]: ...
    async def delete_by_trajectory(self, account_id: str, trajectory_id: str) -> None: ...
    async def update_filter_fields(self, account_id: str, trajectory_id: str, fields: dict) -> None: ...
```

### 2) GraphStore（抽象）
```python
class GraphStore(Protocol):
    async def upsert_raw_clean(self, traj: Trajectory, raw: GraphData, clean: GraphData) -> GraphPointer: ...
    async def get_nodes_edges(self, pointer: GraphPointer, graph_kind: str, node_ids: list[str] | None = None) -> GraphData: ...
    async def match_similar_subgraph(self, query_graph: GraphData, account_id: str, top_k: int, use_raw: bool = False) -> list[GraphHit]: ...
    async def mark_stale(self, account_id: str, trajectory_ids: list[str], reason: str) -> int: ...
```

### 3) TrajectoryRepository（FS）
```python
class TrajectoryRepository(Protocol):
    async def put_trajectory_bundle(self, bundle: TrajectoryBundle) -> None: ...
    async def get_trajectory_meta(self, account_id: str, trajectory_id: str) -> TrajectoryMeta: ...
    async def get_overview(self, uri: str) -> str: ...
    async def put_audit(self, entry: AMCAuditEntry) -> None: ...
```

---

## 10.5 核心交互流程

### Commit 主链路
```text
API(commit)
  -> ACL(commit)
  -> CommitOrchestrator
      -> validate -> normalize -> pair
      -> build raw -> derive clean
      -> summarize(L0/L1)
      -> GraphStore.upsert_raw_clean
      -> TrajectoryRepository.put_trajectory_bundle
      -> EventLog.append(TrajectoryCommitted)
      -> Async Indexer upsert to VectorStore
      -> AuditLogger.write
  -> Response(trajectory_id, nodes, edges, warnings)
```

### Retrieve 主链路
```text
API(retrieve)
  -> ACL(retrieve)
  -> RetrieveOrchestrator
      -> parse query
      -> semantic recall (VectorStore)
      -> [optional] graph recall (GraphStore)
      -> union + rerank(+feedback_boost)
      -> fetch FS L1 + graph evidence
      -> MaskEngine.apply
      -> AuditLogger.write
  -> Response(items + rationale + evidence)
```

### Feedback 回写链路
```text
API(feedback)
  -> FeedbackOrchestrator
      -> validate outcome
      -> update quality scores
      -> update retrieval feature cache/index fields
      -> append event + audit
```

### Propagation 链路（MVP）
```text
ChangeEvent(skill/table/tool)
  -> PropagationOrchestrator
      -> reverse dep query
      -> mark_stale(trajectory + graph + vector filter fields)
      -> append event + audit
```

---

## 10.6 跨模块契约（必须统一）

1. `trajectory_id/node_id/edge_id` 为确定性 ID，避免跨后端漂移；
2. `account_id` 必须贯穿所有索引和查询条件；
3. `lifecycle_status + stale_flag` 同步更新 FS/Vector/Graph；
4. retrieve 返回必须包含 evidence（至少 `trajectory_id + matched_nodes`）；
5. 所有 write/read 动作必须记录审计条目（含 result）。

---

## 10.7 开发态技术选型落地

### Vector（pgvector）
- 用 metadata 存 `account_id/scope/owner_space/task_type/stale_flag/lifecycle_status`；
- 仅索引 `.abstract.md` 与 `.overview.md`；
- 使用确定性主键做 upsert，保证幂等。

### Graph（Neo4j）
- 标签建议：`Trajectory`, `ActionNode`, `DEPENDS_ON`, `BELONGS_TO`；
- raw/clean 用 `graph_kind` 属性或分图命名空间区分；
- 预留 `confidence`, `dep_type`, `stale_flag` 属性用于检索与传播。

---

## 10.8 分阶段编码建议（与 09 对齐）

### Phase 1（M1）
- 完成 `commit` 全链路；
- 跑通 `Neo4j + FS + pgvector` 写入；
- 提供单轨迹回放查询能力。

### Phase 2（M2）
- 完成 `retrieve` 双路召回与融合打分；
- 支持 evidence 回源；
- 接入 ACL + Mask 基线。

### Phase 3（M3）
- 接入 `feedback` 与 `mark_stale`；
- 质量分回写参与 rerank；
- 生命周期过滤可用。

### Phase 4（M4）
- 增加 workflow 聚类与模板草案生成；
- 支持 workflow 优先召回。

---

## 10.9 最小可运行闭环（建议验收）

1. 发送 sample trajectory 到 `/commit`，返回 accepted；
2. Neo4j 可见 raw/clean 节点边；
3. FS 可见 `trajectory.json + graph_pointer + .abstract + .overview + .deps`；
4. pgvector 可检索该 trajectory 的 L0/L1；
5. 用 task + partial trajectory 调 `/retrieve`，返回 evidence；
6. 提交 `/feedback(adopted)` 后，下一次 retrieve 排序有可观测变化；
7. 触发 skill breaking 事件后，目标 trajectory `stale_flag=true` 且默认降权。

---

## 10.10 文档拆分说明

为避免 `10-main-code-structure.md` 过于臃肿，以下内容已拆分为独立文档：

1. 主要代码示意（commit/retrieve/feedback/API 路由）  
   -> `11-core-code-logic-examples.md`
2. 配置规范（`config.yaml` / `.env` / 配置校验）  
   -> `12-configuration-spec.md`

建议阅读顺序：

`10-main-code-structure.md -> 11-core-code-logic-examples.md -> 12-configuration-spec.md`

---

该结构用于指导工程实现，不替代 `02~09` 的语义规范；若冲突，以 `AMC_plan` 的规范文档为准。
