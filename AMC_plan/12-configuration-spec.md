# 12 — AMC 配置规范（config.yaml + .env）

本文件承接 `10-main-code-structure.md`，定义 AMC 的配置分层与模板。

**原则**

- 敏感项（密钥、URI 密码）放 `.env`；业务参数放 `config.yaml`。
- 合并优先级：`进程环境变量 > .env > config.yaml > 代码默认值`。
- 下文 **config.yaml 仅保留当前 AMC 规划中会实际用到的键**；Phase 3+ 能力在文末用「可选扩展」集中列出，避免与 MVP 混淆。

---

## 12.1 配置分层

| 来源 | 内容 |
|------|------|
| `config.yaml` | 服务端口、存储后端类型、commit/retrieve 行为、审计开关等非密钥 |
| `.env` | Neo4j 密码、Embedding API Key、Chroma 路径等 |

---

## 12.2 config.yaml（MVP / 开发态，带注释）

说明：YAML 中 `#` 后为注释。

```yaml
# ---------------------------------------------------------------------------
# 应用进程
# ---------------------------------------------------------------------------
app:
  env: dev                    # 运行环境：dev | staging | prod（用于日志前缀、是否暴露详细错误等）
  host: "0.0.0.0"             # HTTP 监听地址
  port: 8000                  # HTTP 端口
  log_level: INFO             # 日志级别：DEBUG | INFO | WARNING | ERROR

# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
api:
  prefix: "/api/v1/amc"       # 路由前缀，需与 OpenAPI / SDK 一致
  max_payload_mb: 20          # 请求体上限（轨迹 JSON 可能较大）

# ---------------------------------------------------------------------------
# 安全（与 `AMC_plan/05-multitenancy-and-access-control.md` 对齐）
# ---------------------------------------------------------------------------
security:
  acl:
    deny_override: true       # true：显式 deny 优先于 allow（必须）
  masking:
    enabled: true             # retrieve 返回前是否做字段脱敏

# ---------------------------------------------------------------------------
# 存储抽象（开发态：LocalFS + Chroma + Neo4j + JSONL 事件）
# ---------------------------------------------------------------------------
storage:
  content_store:
    backend: localfs          # 轨迹文件落盘：仅 localfs 在 MVP 实现
    localfs_root: "./data/content"  # ctx 映射根目录（实现层约定子路径）

  vector_store:
    backend: chroma           # 语义召回；生产可换 milvus / qdrant
    collection_name: "amc_trajectory_index"  # Chroma collection 名
    distance: cosine          # 向量距离度量，需与召回实现一致
    search_timeout_ms: 2000   # 单次向量检索超时（毫秒）

  graph_store:
    backend: neo4j            # 轨迹 raw/clean 图；AMC 必选
    database: neo4j           # Neo4j 库名（5.x 常用 neo4j）
    query_timeout_ms: 5000   # Cypher 超时（毫秒）

  event_log:
    backend: jsonl            # TrajectoryCommitted 等事件；生产可换 Kafka
    jsonl_path: "./data/events/amc_events.jsonl"

# ---------------------------------------------------------------------------
# 异步向量化（commit 后索引 L0/L1）
# ---------------------------------------------------------------------------
indexing:
  async_enabled: true         # true：先入队再 embed，不阻塞 commit 返回
  include_levels: [0, 1]      # 仅索引 .abstract.md(0) 与 .overview.md(1)

# ---------------------------------------------------------------------------
# Commit（与 AMC_plan/03 对齐）
# ---------------------------------------------------------------------------
commit:
  idempotency:
    enabled: true             # 是否启用 tenant+task+hash 幂等
    ttl_hours: 168            # 幂等键保留时间（小时）
  normalize:
    max_action_result_chars: 12000   # Tool 输出过长时截断，避免图节点爆内存
  graph:
    temporal_fallback_edge: true   # 识别不到输出->输入依赖时是否加 low-confidence temporal 边
    min_edge_confidence: 0.2        # 低于此置信度的边不参与高权重图相似分
  incremental:
    enabled: true             # 是否允许 is_incremental 多次追加同 trajectory

# ---------------------------------------------------------------------------
# Retrieve（与 AMC_plan/04 对齐）
# ---------------------------------------------------------------------------
retrieve:
  top_k_default: 5            # 默认返回条数
  top_k_max: 20               # 上限，防止滥用
  semantic_top_n: 50          # 向量召回候选池大小
  graph_top_n: 50             # 有 partial_trajectory 时图召回候选池大小
  rerank:
    # 有 partial_trajectory：语义 + 图 + 反馈，权重和须为 1
    with_partial:
      w_sem: 0.45
      w_graph: 0.45
      w_fb: 0.10
    # 无 partial_trajectory：仅语义 + 反馈
    without_partial:
      w_sem: 0.90
      w_fb: 0.10
  stale:
    exclude_by_default: true  # true：默认过滤 stale_flag=true 的轨迹

# ---------------------------------------------------------------------------
# 审计（与 `AMC_plan/05-multitenancy-and-access-control.md` 中的审计要求对齐）
# ---------------------------------------------------------------------------
audit:
  enabled: true
  file_path: "./data/audit/amc_audit.log"
  redact_query_text: true     # true：审计不落明文 query，只落 hash

# ---------------------------------------------------------------------------
# 健康检查（部署探活）
# ---------------------------------------------------------------------------
health:
  path: "/healthz"
```

**已从 MVP 主模板删除的项（原因）**

| 原键 | 原因 |
|------|------|
| `app.name`, `timezone` | 非功能必需，可由 env 或常量代替 |
| `api.request_timeout_sec`, `enable_openapi` | 属部署/网关层，可由 Uvicorn/反代配置 |
| `security.auth_enabled`, `default_deny` | 鉴权模式由 ContextHub 统一接入时再定 |
| `security.acl.cache_ttl_sec`, `masking.max_mask_latency_ms_p95` | 调优指标，非配置契约 |
| `vector_store.embedding_dim`, `top_n_default` | 维度由 embedding 模型决定；候选数已用 `retrieve.semantic_top_n` |
| `graph_store.max_connection_pool_size` | 驱动默认值通常足够，需调优时再加 |
| `indexing.queue_backend`, `batch_size`, `retry` | 开发态 inmemory 即可；生产再拆文件 |
| `commit.graph.build_raw/build_clean` | 规划要求双图，无需开关 |
| `retrieve` 下重复的语义/图 top_n 命名 | 统一到 `semantic_top_n` / `graph_top_n` |
| `feedback` / `propagation` / `lifecycle` 整段 | Phase 3+，见 12.2 附录 |
| `observability` / `feature_flags` | 未与单一代码路径绑定时易腐烂，需要时再启用 |

### 附录 A：Phase 3+ 可选扩展（需要实现时再抄入 config.yaml）

```yaml
# feedback:          # 反馈闭环：quality_score、rerank 中的 w_fb 依赖实现后再开
#   enabled: true
# propagation:       # mark_stale：依赖 ReverseDependency 与事件消费
#   enabled: true
# lifecycle:         # active/cold/archived：需定时任务
#   enabled: true
```

---

## 12.3 .env（开发态，带注释）

```bash
# ---------- 运行 ----------
AMC_ENV=dev
AMC_LOG_LEVEL=INFO

# ---------- 鉴权（与 ContextHub 网关约定；MVP 可先用 API Key）----------
# AMC_API_KEY=...
# AMC_JWT_SECRET=...        # 若走 JWT，由上层颁发

# ---------- Content：轨迹文件根目录（与 storage.content_store.localfs_root 一致）----------
AMC_CONTENT_LOCAL_ROOT=./data/content

# ---------- Chroma：持久化目录（嵌入式模式）----------
AMC_CHROMA_PERSIST_DIR=./data/chroma

# ---------- Neo4j（图存储，AMC 必需）----------
AMC_NEO4J_URI=bolt://127.0.0.1:7687
AMC_NEO4J_USER=neo4j
AMC_NEO4J_PASSWORD=请替换为本地开发密码
AMC_NEO4J_DATABASE=neo4j

# ---------- Embedding：用于 L0/L1 向量化（indexing.async_enabled=true 时必需）----------
AMC_EMBEDDING_PROVIDER=openai
AMC_EMBEDDING_MODEL=text-embedding-3-small
AMC_OPENAI_API_KEY=请替换

# ---------- 事件与审计路径（与 config.yaml 中路径保持一致即可）----------
AMC_EVENT_JSONL_PATH=./data/events/amc_events.jsonl
AMC_AUDIT_FILE=./data/audit/amc_audit.log
```

**已删除的 .env 项（原因）**

| 原变量 | 原因 |
|--------|------|
| `AMC_CHROMA_MODE` | 与 `AMC_CHROMA_PERSIST_DIR` 同时出现时冗余；实现上「有持久目录即 persistent」即可 |
| `AMC_QUEUE_BACKEND` | MVP 默认进程内队列，与 yaml `indexing` 合并后再暴露 |
| 重复的 Chroma HTTP 示例 | 开发以嵌入式为主；需 HTTP 时在实现文档中补一行 `CHROMA_URL` |

---

## 12.4 Pydantic 配置对象（与 12.2 对齐，附字段说明）

```python
from pydantic import BaseModel, Field


class VectorStoreConfig(BaseModel):
    """Chroma（或可替换后端）连接与集合名。"""
    backend: str = "chroma"
    collection_name: str = "amc_trajectory_index"
    distance: str = "cosine"
    search_timeout_ms: int = 2000


class GraphStoreConfig(BaseModel):
    """Neo4j（或可替换后端）。"""
    backend: str = "neo4j"
    database: str = "neo4j"
    query_timeout_ms: int = 5000


class RetrieveWeightConfig(BaseModel):
    """融合打分权重；without_partial 无图分时 w_graph 可为 None。"""
    w_sem: float
    w_graph: float | None = None
    w_fb: float


class AppConfig(BaseModel):
    """应用根配置：仅包含当前 yaml 中存在的嵌套段。"""
    env: str = "dev"
    api_prefix: str = "/api/v1/amc"
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    graph_store: GraphStoreConfig = Field(default_factory=GraphStoreConfig)
    retrieve_with_partial: RetrieveWeightConfig = Field(
        default_factory=lambda: RetrieveWeightConfig(w_sem=0.45, w_graph=0.45, w_fb=0.10)
    )
    retrieve_without_partial: RetrieveWeightConfig = Field(
        default_factory=lambda: RetrieveWeightConfig(w_sem=0.90, w_graph=None, w_fb=0.10)
    )
```

---

## 12.5 MVP 启动前必检项

1. `storage.vector_store.backend` / `storage.graph_store.backend` 与实现一致  
2. `AMC_NEO4J_*` 可连通  
3. `indexing.async_enabled=true` 时：`AMC_EMBEDDING_*` 与 API Key 已配置  
4. `retrieve.rerank` 两组权重各自之和为 `1.0`  
5. `retrieve.top_k_default <= retrieve.top_k_max`  
6. `security.acl.deny_override` 与 `security.masking.enabled` 符合企业策略  

---

## 12.6 启动时校验建议（fail-fast）

- Neo4j：URI 不可达则进程退出（或降级模式显式打印）  
- 异步索引开启但缺少 Embedding 密钥：退出  
- rerank 权重和 ≠ 1：退出  
- `top_k_default > top_k_max`：退出  
- 启动后打印脱敏后的有效配置摘要  
