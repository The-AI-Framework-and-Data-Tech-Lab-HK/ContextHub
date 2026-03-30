# 08 — AMC 系统架构

## 架构图（逻辑）

```
┌───────────────────────────────────────────────────────────┐
│                    Upper-level Agent / ContextHub         │
│   (send trajectory to commit)   (query + partial traj)    │
└───────────────────────┬───────────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────────┐
│                      AMC API Layer                        │
│   /commit   /retrieve   /feedback   /promote   /replay    │
└───────────────────────┬───────────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────────┐
│                    AMC Core Services                      │
│                                                           │
│  ┌───────────────┐  ┌────────────────┐  ┌──────────────┐ │
│  │ Trajectory    │  │ Graph Builder  │  │ Deps Manager  │ │
│  │ Normalizer    │  │ + Similarity   │  │ + Event Emit  │ │
│  └──────┬────────┘  └───────┬────────┘  └──────┬───────┘ │
│         │                    │                  │         │
│  ┌──────▼────────┐  ┌────────▼────────┐  ┌──────▼───────┐ │
│  │ Semantic      │  │ Hybrid Reranker │  │ Feedback      │ │
│  │ Indexer       │  │ (sem+graph+fb)  │  │ Updater       │ │
│  └──────┬────────┘  └────────┬────────┘  └──────┬───────┘ │
└─────────┼─────────────────────┼──────────────────┼─────────┘
          ▼                     ▼                  ▼
┌───────────────────────────────────────────────────────────┐
│                    Storage Abstraction                    │
│  Memory FS Store   Graph Store   Vector Store   Event Log  │
└───────────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 职责 |
|------|------|
| AMC API | 对外提供 commit/retrieve/feedback 接口，做鉴权与参数校验 |
| Trajectory Normalizer | 将原始轨迹标准化为节点候选 |
| Graph Builder | 识别依赖并构建图；提供图特征提取与相似度计算 |
| Semantic Indexer | 生成 trajectory-level L0/L1，向量化与标量索引维护 |
| Hybrid Reranker | 融合语义分、图分、反馈分，输出 top-k |
| Deps Manager | 写入 `.deps.json`、维护反向依赖索引、发出 ChangeEvent |
| Feedback Updater | 回收 adopted/ignored/corrected，更新质量分 |
| Event Log | append-only 事件存储，供审计与重放 |

## Semantic Indexer 设计（实现细节）

在 AMC 中，Semantic Indexer 采用“FS 权威 + 向量副本”的模式，参考 OpenViking 的 `Semantic -> EmbeddingQueue -> Upsert` 机制：

- FS 是事实来源（`trajectory.json/.abstract.md/.overview.md`）；
- 向量库是检索副本（仅用于 ANN 召回）；
- 图后端只负责结构检索与证据，不参与文本向量化。

### 数据流

```
TrajectoryCommitted
   -> Indexer 读取 FS 的 L0/L1
   -> 生成 IndexDoc(level=0/1)
   -> EmbeddingQueue
   -> Embedding Worker
   -> Vector Store Upsert
```

### 索引范围

- 当前仅 trajectory-level：
  - `.../.abstract.md` (L0)
  - `.../.overview.md` (L1)
- node-level 文本索引后续再扩展。

### 向量记录关键字段

- `id`（确定性：`md5(account_id + seed_uri)`）
- `uri`, `parent_uri`, `level`
- `account_id`, `scope`, `owner_space`
- `trajectory_id`, `agent_id`, `task_type`
- `lifecycle_status`（active/cold/archived/deleted）
- `stale_flag`（是否因传播被标记 stale）
- `vector`, `created_at`, `updated_at`

### 增量维护接口

- `upsert_trajectory_index(trajectory_uri)`
- `delete_trajectory_index(trajectory_uri)`
- `update_status_filter_fields(trajectory_id, lifecycle_status, stale_flag)`

其中 `lifecycle_status + stale_flag` 共同影响 retrieve 过滤和降权策略（如 `archived/deleted` 默认不召回，`stale_flag=true` 默认降权/过滤）。

## 反向依赖索引（用于变更传播）

用途：在 skill/schema/tool 变更时，快速定位受影响 trajectory，避免全库扫描。

推荐索引结构（逻辑）：
```python
class ReverseDependency:
    account_id: str                 # 账户隔离字段（避免跨账户误命中）
    dep_uri: str                    # 被依赖对象 URI（如某 skill URI）
    trajectory_id: str              # 依赖该对象的轨迹 ID
    dep_type: str                   # 依赖类型：skill_version | table_schema | tool_behavior
    dep_version: str | None         # 依赖绑定版本（可选；MVP 可不落该字段）
    updated_at: datetime            # 最近更新时间
```

核心接口：
- `upsert_reverse_deps(trajectory_id, deps)`
- `query_dependents(dep_uri, account_id)`
- `delete_reverse_deps(trajectory_id)`

## 推荐存储技术（可替换）

开发态：
- Content: LocalFS
- Graph: Neo4j（单机）
- Vector: pgvector
- Event Log: append-only JSONL

生产态：
- Content: S3/OSS
- Graph: Neo4j / JanusGraph / pgvector+边表
- Vector: Milvus / Qdrant
- Event: Kafka / Redis Streams

## 关键工程决策

1. 图存储与向量存储分离（避免强耦合）；
2. 文件系统仅保存 trajectory-level pointer 与 L0/L1，不存图节点/边；
3. 检索路径并行（semantic 与 graph 并发）；
4. 所有检索结果必须附 evidence（可解释）；
5. 事件优先异步化（commit 成功不阻塞全部下游）。

