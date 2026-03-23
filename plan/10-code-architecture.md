# 10 — 代码架构设计

基于 01-09 设计文档，解决从设计到代码的 5 个关键缺失：项目结构、依赖注入、API 端点、向量索引抽象（pgvector）、L0/L1 生成。

---

## 一、项目目录结构

```
contexthub/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial_schema.py
├── src/
│   └── contexthub/
│       ├── __init__.py
│       ├── main.py                    # FastAPI app 工厂 + lifespan
│       ├── config.py                  # pydantic-settings
│       │
│       ├── models/                    # Pydantic 数据模型
│       │   ├── context.py             # Context, ContextLevel, ContextType, ContextStatus
│       │   ├── request.py             # RequestContext, SearchRequest/Response
│       │   ├── datalake.py            # TableMetadata, Lineage, TableRelationship, QueryTemplate
│       │   ├── skill.py               # SkillVersion, SkillSubscription
│       │   ├── memory.py              # ContextFeedback, MemoryCategory
│       │   ├── access.py              # AccessPolicy
│       │   └── team.py                # TeamMembership
│       │
│       ├── db/                        # 数据库层
│       │   ├── pool.py                # asyncpg 连接池（create_pool / close_pool）
│       │   ├── repository.py          # PgRepository：封装 asyncpg，提供 fetch/execute/transaction
│       │   └── queries/               # 原始 SQL 常量（按领域分文件）
│       │       ├── contexts.py
│       │       ├── dependencies.py
│       │       ├── events.py
│       │       ├── datalake.py
│       │       ├── skills.py
│       │       ├── access.py
│       │       ├── audit.py
│       │       ├── feedback.py
│       │       ├── lifecycle.py
│       │       └── teams.py
│       │
│       ├── store/                     # URI 路由层
│       │   └── context_store.py       # ContextStore: read/write/search/ls/stat
│       │
│       ├── services/                  # 业务服务层
│       │   ├── context_service.py     # CRUD、L0/L1/L2 管理
│       │   ├── memory_service.py      # 提取、去重、热度、共享提升
│       │   ├── skill_service.py       # 版本管理、发布/订阅
│       │   ├── retrieval_service.py   # 向量检索 L0 → PG 读 L1 rerank → 加载 L2
│       │   ├── indexer_service.py     # 异步 L0/L1 生成 + 向量索引更新
│       │   ├── propagation_engine.py  # PG LISTEN/NOTIFY → 查依赖 → 执行规则
│       │   ├── acl_service.py         # check_access / filter_and_mask
│       │   ├── audit_service.py       # 写审计日志
│       │   ├── feedback_service.py    # 反馈采集、质量评分
│       │   ├── lifecycle_service.py   # 状态机、定时归档
│       │   └── catalog_sync_service.py # CatalogConnector → PG 同步
│       │
│       ├── propagation/               # 变更传播规则
│       │   ├── base.py                # PropagationRule ABC, PropagationAction
│       │   ├── skill_version_rule.py  # Level 1: 纯规则
│       │   ├── table_schema_rule.py   # Level 1/2: 自动重新生成
│       │   ├── derived_memory_rule.py # Level 2: 模板替换
│       │   ├── complex_rule.py        # Level 3: LLM 推理
│       │   └── registry.py            # dep_type → Rule 映射
│       │
│       ├── connectors/                # 外部数据源
│       │   ├── base.py                # CatalogConnector ABC
│       │   └── mock_connector.py      # 开发用 Mock
│       │
│       ├── vector/                    # 向量索引抽象
│       │   ├── base.py                # VectorStore ABC
│       │   ├── pgvector_store.py      # pgvector 实现（与 PG 同库）
│       │   └── factory.py             # create_vector_store()
│       │
│       ├── llm/                       # LLM 调用抽象
│       │   ├── base.py                # LLMClient ABC + EmbeddingClient ABC
│       │   ├── openai_client.py       # OpenAI embedding + chat
│       │   └── factory.py             # create_llm_client() / create_embedding_client()
│       │
│       ├── generation/                # L0/L1 内容生成
│       │   ├── base.py                # ContentGenerator + GenerationStrategy ABC
│       │   ├── table_schema.py        # TableSchemaStrategy（LLM）
│       │   ├── skill.py               # SkillStrategy（纯模板）
│       │   ├── memory.py              # MemoryStrategy（模板 + 可选 LLM）
│       │   └── resource.py            # ResourceStrategy（LLM）
│       │
│       ├── retrieval/                # 检索策略层（可插拔）
│       │   ├── router.py             # RetrievalRouter：按 context_type 分发检索策略
│       │   ├── vector_strategy.py    # 默认策略：向量检索 L0 → PG L1 rerank
│       │   ├── rerank.py             # RerankStrategy ABC + KeywordRerankStrategy（BM25）
│       │   ├── tree_strategy.py      # 长文档树导航检索（Phase 2 可选）
│       │   └── keyword_strategy.py   # 长文档 ripgrep 关键词检索（Phase 2 可选）
│       │
│       ├── ingestion/                # 内容入库管线
│       │   └── long_document.py      # LongDocumentIngester（Phase 2 可选）
│       │
│       └── api/                       # FastAPI 路由
│           ├── deps.py                # Depends 工厂函数
│           ├── middleware.py           # 认证中间件（含 RLS SET LOCAL）
│           └── routers/
│               ├── contexts.py        # /api/v1/contexts
│               ├── search.py          # /api/v1/search
│               ├── memories.py        # /api/v1/memories
│               ├── skills.py          # /api/v1/skills
│               ├── datalake.py        # /api/v1/datalake
│               ├── tools.py           # /api/v1/tools（LLM tool use: ls/read/grep/stat）
│               └── admin.py           # /api/v1/admin
│
├── sdk/                               # Python SDK（独立可发布包）
│   ├── pyproject.toml
│   └── src/
│       └── contexthub_sdk/
│           ├── __init__.py
│           ├── client.py              # ContextHubClient：HTTP 封装
│           ├── models.py              # SDK 侧 Pydantic 模型
│           └── exceptions.py
│
├── plugins/                           # Agent 框架插件
│   └── openclaw/                      # OpenClaw Plugin
│       ├── pyproject.toml
│       ├── plugin.py                  # 注册 tools + lifecycle hooks
│       └── tools.py                   # contexthub_search/store/promote/feedback
│
├── docker-compose.yml                 # PG (with pgvector) + ContextHub Server
├── docker-compose.dev.yml             # 开发环境覆盖（端口映射、volume 挂载）
│
└── tests/
    ├── conftest.py
    ├── test_context_store.py
    ├── test_retrieval.py
    ├── test_propagation.py
    └── ...
```

模块边界原则：
- `models/` 纯数据定义，无业务逻辑，无 IO
- `db/` 只做 SQL 执行，不含业务判断
- `store/` 是 URI 路由层，协调 PG（含 pgvector）+ ACL
- `services/` 是业务逻辑层，依赖 `store/` 和 `db/`
- `retrieval/` 是检索策略层，可插拔。`services/retrieval_service.py` 调用 `retrieval/router.py`
- `ingestion/` 是内容入库管线，处理外部内容（长文档等）的预处理和写入
- `api/` 只做 HTTP 协议转换，不含业务逻辑
- `sdk/` 是独立包，只依赖 HTTP + Pydantic，不依赖 server 内部模块
- `plugins/` 是 Agent 框架适配层，依赖 `sdk/`

---

## 二、依赖注入与服务装配

### 2.1 config.py

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model_config = {"env_prefix": "CTX_", "env_file": ".env"}

    # PostgreSQL
    pg_dsn: str = "postgresql://ctx:ctx@localhost:5432/contexthub"
    pg_min_pool: int = 5
    pg_max_pool: int = 20

    # 向量索引（pgvector，与 PG 同库，无额外配置）
    embedding_index_type: str = "hnsw"   # "hnsw" | "ivfflat"

    # LLM
    llm_backend: str = "openai"          # "openai" | "anthropic"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # CatalogConnector
    catalog_backend: str = "mock"        # "mock" | "hive" | "iceberg"

    # 传播引擎
    propagation_enabled: bool = True

    # Rerank 策略
    rerank_strategy: str = "keyword"     # "keyword" | "cross_encoder" | "llm"

    # pgvector embedding 一致性检查
    reconcile_interval_minutes: int = 60  # 0 = 禁用

    # 长文档（Phase 2 可选）
    doc_store_root: str = "/data/docs"
    long_document_enabled: bool = False
```

### 2.2 服务依赖图

```
Settings → asyncpg.Pool → PgRepository
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         VectorStore     LLMClient     CatalogConnector
         EmbeddingClient
              │               │
              ▼               ▼
         ACLService      ContentGenerator
         AuditService    (table_schema/skill/memory/resource strategies)
              │               │
              ▼               ▼
         ContextStore    IndexerService
              │               │
    ┌─────┬──┴──┬─────┐      │
    ▼     ▼     ▼     ▼      ▼
 Context Memory Skill Retrieval  PropagationEngine
 Service Service Service Service  (background task)
```

### 2.3 main.py — App Factory + Lifespan

所有 service 在 lifespan 中一次性装配，通过构造函数注入依赖。不使用 DI 框架。

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()

    # 基础设施
    pool = await create_pool(settings)
    repo = PgRepository(pool)
    vector_store = create_vector_store(settings)
    await vector_store.initialize()
    llm_client = create_llm_client(settings)
    embedding_client = create_embedding_client(settings)
    catalog_connector = create_catalog_connector(settings)

    # 横切关注点
    acl_service = ACLService(repo)
    audit_service = AuditService(repo)

    # 核心层
    content_generator = ContentGenerator(llm_client)
    context_store = ContextStore(repo, vector_store, acl_service, audit_service)
    indexer_service = IndexerService(repo, vector_store, embedding_client, content_generator)
    retrieval_service = RetrievalService(repo, vector_store, embedding_client, acl_service)

    # 业务服务
    context_service = ContextService(repo, context_store, indexer_service)
    memory_service = MemoryService(repo, context_store, indexer_service)
    skill_service = SkillService(repo, context_store, indexer_service)
    catalog_sync_service = CatalogSyncService(repo, catalog_connector, indexer_service, llm_client)

    # 传播引擎（后台 asyncio task）
    rule_registry = PropagationRuleRegistry.default(indexer_service, llm_client)
    propagation_engine = PropagationEngine(settings.pg_dsn, pool, rule_registry)

    # 挂到 app.state，供 Depends 使用
    app.state.context_service = context_service
    app.state.memory_service = memory_service
    app.state.skill_service = skill_service
    app.state.retrieval_service = retrieval_service
    # ... 其余 service 同理

    if settings.propagation_enabled:
        await propagation_engine.start()

    yield

    await propagation_engine.stop()
    await pool.close()
```

### 2.4 api/deps.py — Depends 链

```python
async def get_request_context(
    x_account_id: str = Header(...),
    x_agent_id: str = Header(...),
    if_match: Optional[int] = Header(None, alias="If-Match"),  # 乐观锁版本号
) -> RequestContext:
    return RequestContext(account_id=x_account_id, agent_id=x_agent_id, expected_version=if_match)

def get_context_service(request: Request) -> ContextService:
    return request.app.state.context_service

# 其余 service 同理：get_memory_service, get_skill_service, ...
```

Endpoint 使用方式：
```python
@router.post("/contexts", status_code=201)
async def create_context(
    body: CreateContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    svc: ContextService = Depends(get_context_service),
):
    return await svc.create(body, ctx)
```

### 2.5 PropagationEngine 后台任务

- 用独立的 asyncpg 连接做 `LISTEN`（不能用连接池，LISTEN 需要长连接）
- `_on_notify` 回调中用 debounce（2 秒窗口）合并同一 URI 的多次 NOTIFY
- debounce 窗口结束后，`process_event` 处理该 URI 的**所有**未处理事件（不是只取最新一条）
- `process_event` 内部捕获异常，单个依赖方处理失败不影响其他依赖方
- 失败的事件保留 `processed = FALSE`，下次启动时自动重试
- MVP 限制：单实例部署。多实例需要 `SELECT FOR UPDATE SKIP LOCKED`
- lifespan shutdown 时 `cancel()` task + 关闭 LISTEN 连接

```python
class PropagationEngine:
    async def start(self):
        self._listen_conn = await asyncpg.connect(self._dsn)
        await self._listen_conn.add_listener("context_changed", self._on_notify)

    def _on_notify(self, conn, pid, channel, payload):
        # debounce: 2 秒内同一 URI 只触发一次处理
        # 注意：debounce 合并的是 NOTIFY，不是事件本身。
        # process_event 会读取该 URI 的所有未处理事件。
        source_uri = payload
        if source_uri in self._pending:
            self._pending[source_uri].cancel()
        loop = asyncio.get_running_loop()
        self._pending[source_uri] = loop.call_later(
            2.0, lambda: asyncio.create_task(self._safe_process(source_uri))
        )

    async def process_event(self, source_uri):
        # 1. 读取该 URI 的所有未处理 change_events（按时间正序）
        # 2. 查询 dependencies WHERE target_uri = source_uri
        # 3. 对每个事件 × 每个依赖方执行 registry.get(dep_type).evaluate()
        # 4. 执行 action: mark_stale / auto_update / notify
        # 5. 每个事件处理完毕后标记 processed = TRUE
        # 详见 06-change-propagation.md (6) 完整实现

    async def stop(self):
        if self._listen_conn:
            await self._listen_conn.close()
```

---

## 三、FastAPI API 端点设计

### 完整路由表

| 方法 | 路径 | 功能 | Router |
|------|------|------|--------|
| **上下文 CRUD** |
| `POST` | `/api/v1/contexts` | 创建上下文 | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}` | 读取（query: `level=L0\|L1\|L2`） | contexts.py |
| `PATCH` | `/api/v1/contexts/{uri:path}` | 更新内容 | contexts.py |
| `DELETE` | `/api/v1/contexts/{uri:path}` | 标记删除 | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}/stat` | 元信息（不含内容） | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}/children` | 列出子项（ls 语义） | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}/deps` | 查看依赖关系 | contexts.py |
| **语义检索** |
| `POST` | `/api/v1/search` | 向量检索 + Rerank | search.py |
| `POST` | `/api/v1/search/sql-context` | Text-to-SQL 上下文组装 | search.py |
| **记忆** |
| `POST` | `/api/v1/memories` | 添加记忆 | memories.py |
| `GET` | `/api/v1/memories` | 列出记忆（query: `category`, `scope`） | memories.py |
| `POST` | `/api/v1/memories/promote` | 提升为团队共享 | memories.py |
| **技能** |
| `POST` | `/api/v1/skills/versions` | 发布新版本 | skills.py |
| `GET` | `/api/v1/skills/{uri:path}/versions` | 版本历史 | skills.py |
| `POST` | `/api/v1/skills/subscribe` | 订阅技能 | skills.py |
| **反馈** |
| `POST` | `/api/v1/feedback` | 记录上下文反馈 | search.py |
| **数据湖** |
| `POST` | `/api/v1/datalake/sync` | 触发 catalog 同步 | datalake.py |
| `GET` | `/api/v1/datalake/{catalog}/{db}` | 列出表 | datalake.py |
| `GET` | `/api/v1/datalake/{catalog}/{db}/{table}` | 表完整上下文 | datalake.py |
| `GET` | `/api/v1/datalake/{catalog}/{db}/{table}/lineage` | 血缘查询 | datalake.py |
| **LLM Tool Use** |
| `POST` | `/api/v1/tools/ls` | 列目录 | tools.py |
| `POST` | `/api/v1/tools/read` | 读内容 | tools.py |
| `POST` | `/api/v1/tools/grep` | 语义搜索 | tools.py |
| `POST` | `/api/v1/tools/stat` | 元信息 | tools.py |
| **管理** |
| `GET` | `/api/v1/admin/quality-report` | 低质量上下文报告 | admin.py |
| `POST` | `/api/v1/admin/lifecycle/run` | 手动触发生命周期 | admin.py |
| `GET` | `/api/v1/admin/propagation/status` | 传播引擎状态 | admin.py |

### 认证方式

通过 HTTP Headers 传递身份信息（MVP 阶段简化）：
- `X-API-Key`: API 密钥
- `X-Account-Id`: 租户 ID
- `X-Agent-Id`: Agent 标识
- `If-Match`: 乐观锁版本号（PATCH 更新时必传，对应 `contexts.version`）

响应中通过 `ETag` 返回当前版本号，客户端下次更新时回传 `If-Match`。版本不匹配返回 `409 Conflict`。

---

## 四、VectorStore 抽象接口

### 核心数据类型

```python
@dataclass(frozen=True)
class VectorRecord:
    uri: str
    account_id: str
    vector: list[float]          # L0 dense embedding
    context_type: str            # table_schema | memory | skill | resource
    parent_uri: str
    owner_space: str
    name: str
    abstract: str                # L0 摘要文本
    tags: str                    # 逗号分隔
    active_count: int = 0
    updated_at: datetime

    @property
    def id(self) -> str:
        """确定性 ID：md5(account_id:uri)，保证幂等写入"""
        return hashlib.md5(f"{self.account_id}:{self.uri}".encode()).hexdigest()

@dataclass
class VectorSearchParams:
    query_vector: list[float]
    account_id: str              # 必须：租户隔离
    top_k: int = 20
    context_type: str | None = None
    scope_owner_spaces: list[str] | None = None  # 可见性已展开的 owner_space 列表
    min_active_count: int | None = None

@dataclass
class VectorSearchResult:
    uri: str
    score: float
    abstract: str
    context_type: str
    owner_space: str
```

### VectorStore ABC

```python
class VectorStore(ABC):
    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def upsert(self, record: VectorRecord) -> None: ...

    @abstractmethod
    async def upsert_batch(self, records: list[VectorRecord]) -> None: ...

    @abstractmethod
    async def search(self, params: VectorSearchParams) -> list[VectorSearchResult]: ...

    @abstractmethod
    async def delete(self, uri: str, account_id: str) -> None: ...

    @abstractmethod
    async def delete_batch(self, uris: list[str], account_id: str) -> None: ...

    @abstractmethod
    async def get_count(self, account_id: str) -> int: ...

    # 非抽象方法：从 PG 全量重建向量索引（逻辑对所有后端一致）
    async def rebuild_from_pg(self, pool, account_id, embed_fn) -> int: ...

    @abstractmethod
    async def close(self) -> None: ...
```

### EmbeddingClient（独立于 LLMClient）

```python
class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dim(self) -> int: ...
```

### 交互流程

```
写入: IndexerService → EmbeddingClient.embed(l0) → VectorRecord → VectorStore.upsert()
检索: RetrievalService → EmbeddingClient.embed(query) → VectorStore.search() → top-K URI → PG 读 L1 rerank
删除: LifecycleService → VectorStore.delete() / delete_batch()
重建: 运维命令 → VectorStore.rebuild_from_pg()
对账: 定时任务 → VectorStoreReconciler.reconcile()
```

### pgvector embedding 一致性检查

由于 pgvector 与内容存在同一个 PG 中，大部分写入可以在同一事务中完成（L0 内容 + embedding 向量同步写入），从根本上消除了双写不一致问题。

但仍需处理 embedding 异步生成的场景（如 L0 由 LLM 生成后、embedding 需要调用外部 API 异步回填）。定时任务检测缺失 embedding 并补写：

```python
class EmbeddingReconciler:
    """定时检查：确保有 l0_content 的 active context 都有对应的 embedding"""

    async def reconcile(self, account_id: str):
        missing = await self.pg.fetch("""
            SELECT uri, l0_content FROM contexts
            WHERE account_id = $1 AND status IN ('active', 'stale')
              AND l0_content IS NOT NULL
              AND l0_embedding IS NULL
        """, account_id)

        if missing:
            logger.info(f"Backfilling {len(missing)} missing embeddings")
            for row in missing:
                embedding = await self.embed_fn(row['l0_content'])
                await self.pg.execute(
                    "UPDATE contexts SET l0_embedding = $1 WHERE uri = $2",
                    embedding, row['uri'])
```

建议运行频率：每小时一次（可配置 `CTX_RECONCILE_INTERVAL_MINUTES=60`）。相比原来的跨系统对账，逻辑大幅简化。

---

## 五、L0/L1 生成实现

### 核心设计：ContentGenerator + Strategy 模式

```python
class ContentGenerator:
    """统一入口，根据 context_type 分派到对应策略"""
    def __init__(self, llm: LLMClient):
        self._strategies = {
            "table_schema": TableSchemaStrategy(llm),
            "skill":        SkillStrategy(),          # 无 LLM 依赖
            "memory":       MemoryStrategy(llm),
            "resource":     ResourceStrategy(llm),
        }

    async def generate(self, context_type: str, raw_content: str, metadata: dict) -> GeneratedContent:
        return await self._strategies[context_type].generate(raw_content, metadata)

@dataclass
class GeneratedContent:
    l0: str                     # ~100 tokens
    l1: str                     # ~2k tokens
    llm_tokens_used: int        # 纯模板为 0
```

### 各类型生成策略

| context_type | L0 生成 | L1 生成 | LLM 调用 | Token 消耗 |
|---|---|---|---|---|
| `table_schema` | LLM：DDL+字段注释 → 一句话业务描述 | 模板拼 schema 表格 + LLM 生成查询模式建议 | 是 | ~500 |
| `skill` | 模板：提取 Markdown 标题 + 首句 | 模板：全文截断到 ~2k tokens | 否 | 0 |
| `memory` | 模板：前 ~100 tokens 截取 | 短内容直接返回；超长才用 LLM 压缩 | 罕见 | 通常 0 |
| `resource` | LLM：文档前 2000 字 → 一句话摘要 | LLM：生成结构化概览（主题/要点/适用场景） | 是 | ~1500 |

### LLMClient ABC

```python
@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str

class LLMClient(ABC):
    @abstractmethod
    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0) -> LLMResponse: ...

    @abstractmethod
    async def complete_batch(self, prompts: list[str], max_tokens: int = 512, max_concurrency: int = 10) -> list[LLMResponse]: ...
```

### TableSchemaStrategy 关键设计

L1 生成分两步，最大化确定性内容、最小化 LLM 调用：
1. 模板拼接 schema 表格 + 分区信息 + 统计信息 + 样例数据（零 token）
2. LLM 只生成"常用查询模式"段落（~200 input + ~200 output tokens）

### SkillStrategy 关键设计

完全不调用 LLM。Skill 本身是 Markdown 指令，截取比摘要更保真：
- L0: 提取 `# 标题` + 正文首句
- L1: 全文截断到 ~3000 字符，在段落边界截断

### MemoryStrategy 关键设计

大多数记忆 < 2k tokens，直接返回全文（零 LLM）：
- L0: 前 ~100 tokens 截取
- L1: 内容 <= 3000 字符直接返回；超长才用 LLM 压缩（罕见）

---

## 六、实施顺序

按 Phase 实施，每个 Phase 内的文件创建顺序：

### Phase 0（基础设施，1 天）
0. `docker-compose.yml` + `Dockerfile` + `.env.example`

### Phase 1（骨架，1-2 周）
1. `pyproject.toml` + `alembic.ini`
2. `config.py` + `models/` 全部
3. `db/pool.py` + `db/repository.py`（含 RLS SET LOCAL 逻辑）+ `db/queries/`
4. `alembic/versions/001_initial_schema.py`（所有核心表 + RLS + 索引）
5. `vector/base.py` + `vector/pgvector_store.py` + `vector/factory.py`
6. `llm/base.py` + `llm/openai_client.py` + `llm/factory.py`
7. `generation/` 全部
8. `retrieval/rerank.py`（KeywordRerankStrategy）
9. `store/context_store.py`（含乐观锁、列名白名单）
10. `services/acl_service.py` + `services/audit_service.py`
11. `services/indexer_service.py`
12. `api/deps.py` + `api/middleware.py`（含 RLS 中间件）+ `api/routers/contexts.py`
13. `main.py`

### Phase 2A（数据湖，3-4 周）
14. `connectors/base.py` + `connectors/mock_connector.py`
15. `services/catalog_sync_service.py`
16. `retrieval/router.py` + `retrieval/vector_strategy.py`
17. `services/retrieval_service.py`
18. `api/routers/datalake.py` + `api/routers/search.py`

### Phase 2B（多 Agent，3-4 周，与 2A 并行）
19. `services/memory_service.py`（含 promote + approve/reject 预留接口）+ `services/skill_service.py`
20. `api/routers/memories.py` + `api/routers/skills.py`

### Phase 2C（变更传播，2A+2B 之后）
21. `propagation/` 全部
22. `services/propagation_engine.py`（含全事件处理、错误隔离、单实例注释）

### Phase 2D（最小权限 + 运维）
23. `services/acl_service.py` 补充 owner_space 多团队可见性检查
24. `services/reconciler_service.py`（pgvector 索引一致性检查）

### Phase 2E（SDK + Plugin，与 2A/2B 并行）
25. `sdk/` 全部
26. `plugins/openclaw/` 全部

### Phase 3（集成与评估，2 周）
27. 两条线集成 + MVP 场景验证
28. ECMB benchmark

---

## 七、验证方式

1. Phase 1 完成后：`uvicorn contexthub.main:app` 启动，`POST /api/v1/contexts` 创建上下文，`GET` 读取
2. Phase 2A 完成后：`POST /api/v1/datalake/sync` 同步 Mock 数据，`POST /api/v1/search` 语义检索
3. Phase 2C 完成后：修改湖表 schema → 观察依赖方被标记 stale
4. 全部完成后：跑通 MVP 场景（自然语言 → 上下文检索 → SQL 生成）

---

## 八、基础设施

### Docker Compose（开发环境）

```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ctx
      POSTGRES_PASSWORD: ctx
      POSTGRES_DB: contexthub
    ports:
      - "5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ctx"]
      interval: 5s
      timeout: 3s
      retries: 5

  contexthub:
    build: .
    ports:
      - "8000:8000"
    environment:
      CTX_PG_DSN: postgresql://ctx:ctx@postgres:5432/contexthub
      CTX_OPENAI_API_KEY: ${OPENAI_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  pg_data:
```

### Alembic Migration 要点

`alembic/versions/001_initial_schema.py` 需包含：
- 所有核心表（contexts, dependencies, change_events, table_metadata, lineage, table_relationships, query_templates, skill_versions, access_policies, audit_log, team_memberships, lifecycle_policies, context_feedback）
- 长文档扩展：`ALTER TABLE contexts ADD COLUMN file_path TEXT`（可选，Phase 2 启用）
- `document_sections` 表（可选，Phase 2 启用）
- RLS 策略 + 索引
- `lifecycle_policies` 默认数据

### 认证中间件中的 RLS 设置

```python
# api/middleware.py
@app.middleware("http")
async def set_rls_context(request: Request, call_next):
    account_id = request.headers.get("X-Account-Id")
    if account_id:
        # 在请求级别设置 RLS 上下文
        # PgRepository.acquire() 内部执行：
        #   await conn.execute("SET LOCAL app.account_id = $1", account_id)
        request.state.account_id = account_id
    response = await call_next(request)
    return response
```
