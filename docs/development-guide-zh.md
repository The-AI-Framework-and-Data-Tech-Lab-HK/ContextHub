# ContextHub — 开发者指南

> API 参考、技术选型和项目结构，面向 ContextHub 贡献者。
>
> 本地开发环境搭建请参考 [本地部署与端到端验证指南](local-setup&end2end-verification-guide-zh.md)。
> OpenClaw 集成请参考 [OpenClaw 集成指南](openclaw-integration-guide-zh.md)。

## 快速开始

### 前置条件

- Python 3.12+
- Docker & Docker Compose（或 macOS 上通过 Homebrew 安装的 PostgreSQL）
- PostgreSQL 16 + pgvector

### 1. 克隆并安装

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
pip install -e ".[dev]"
```

### 2. 启动 PostgreSQL

```bash
docker compose up -d
```

启动 PostgreSQL 16 + pgvector，端口 5432（用户：`contexthub`，密码：`contexthub`，数据库：`contexthub`）。

macOS 用户如不使用 Docker，请参考 [本地部署指南](local-setup&end2end-verification-guide-zh.md) 中的 Homebrew 安装方式。

### 3. 执行数据库迁移

```bash
alembic upgrade head
```

### 4. 启动服务

```bash
uvicorn contexthub.main:app --reload
```

API 地址：`http://localhost:8000`，OpenAPI 文档：`/docs`。

## API 概览

所有请求需携带 `X-Account-Id`、`X-Agent-Id` 和 `X-API-Key` 请求头以实现租户隔离和认证。

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/contexts` | 创建上下文 |
| GET | `/api/v1/contexts/{uri}` | 读取上下文（Skill 自动走版本解析） |
| PATCH | `/api/v1/contexts/{uri}` | 更新上下文（`If-Match` 乐观锁） |
| DELETE | `/api/v1/contexts/{uri}` | 逻辑删除 |
| POST | `/api/v1/search` | 统一语义检索 |
| POST | `/api/v1/memories` | 添加私有记忆 |
| POST | `/api/v1/memories/promote` | 晋升记忆到团队范围 |
| POST | `/api/v1/skills/versions` | 发布 Skill 新版本 |
| POST | `/api/v1/skills/subscribe` | 订阅 Skill |
| POST | `/api/v1/tools/{ls,read,grep,stat}` | Agent 工具调用端点 |

## 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| Web 框架 | FastAPI | 异步、类型安全、自动生成 OpenAPI |
| 数据库 | PostgreSQL 16 | 元数据 + 内容 + 向量 + 事件统一存储 |
| 向量检索 | pgvector | 同库同事务，无双写对账问题 |
| 异步驱动 | asyncpg | 高性能异步 PG 客户端，原生 LISTEN/NOTIFY |
| 数据库迁移 | Alembic | Schema 版本管理 |
| Embedding | text-embedding-3-small (1536维) | L0 摘要级别，成本效果平衡 |
| HTTP 客户端 | httpx | 轻量异步 HTTP，用于 embedding API 调用 |
| 数据校验 | Pydantic v2 | 请求/响应模型自动校验 |

## 项目结构

```
contexthub/
├── src/contexthub/
│   ├── api/              # FastAPI 路由 + 中间件 + 依赖注入
│   ├── db/               # PgRepository、ScopedRepo（request-scoped 数据库执行器）
│   ├── models/           # Pydantic 模型
│   ├── services/         # 业务逻辑（记忆、技能、检索、传播、ACL）
│   ├── store/            # ContextStore（URI 路由：read/write/ls/stat）
│   ├── retrieval/        # 检索策略（向量、关键词、BM25 精排）
│   ├── propagation/      # 变更传播规则（skill_dep、table_schema、derived_from）
│   ├── generation/       # L0/L1 内容生成
│   ├── llm/              # Embedding 客户端抽象（OpenAI、NoOp）
│   └── connectors/       # Catalog 连接器（MVP 使用 Mock）
├── sdk/                  # Python SDK（typed HTTP 客户端）
├── plugins/openclaw/     # OpenClaw context-engine 插件
├── bridge/               # TS bridge + Python sidecar（OpenClaw ↔ ContextHub）
├── alembic/              # 数据库迁移
├── tests/                # 集成测试（可见性、传播、检索等）
├── plan/                 # 设计文档（15 篇，从不变式到实施计划）
└── docs/                 # 部署指南、验证计划、集成指南
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `api/` | HTTP 层：路由处理、租户域中间件（`X-Account-Id` → `SET LOCAL`）、依赖注入 |
| `db/` | `PgRepository`（原始 asyncpg 连接池）+ `ScopedRepo`（request-scoped 执行器，自动设置 `app.account_id`） |
| `store/` | `ContextStore` — `ctx://` URI 路由器。将 `read/write/ls/stat` 操作映射到 PostgreSQL 查询 |
| `services/memory_service.py` | 添加、列表、晋升记忆，`derived_from` 血缘追踪 |
| `services/skill_service.py` | 发布版本、订阅、解析 `pinned`/`latest`/显式版本 |
| `retrieval/` | `VectorStrategy`（pgvector）、`KeywordStrategy`（ILIKE 降级）、`BM25Reranker`、ACL 过滤 |
| `propagation/` | Outbox 消费循环、三级规则分发、指数退避重试、NOTIFY + 周期补扫 |
| `services/acl_service.py` | 默认可见性（递归 CTE 团队层级展开）+ 写权限检查 |
| `generation/` | L0 摘要 + L1 结构化概览生成（通过 LLM 或模板） |
| `connectors/` | `CatalogConnector` 接口 + `MockCatalogConnector`（MVP） |

### Bridge 架构（OpenClaw 集成）

```
bridge/
├── openclaw.plugin.json     # 插件清单（kind: "context-engine"，slot: exclusive）
├── package.json             # npm 包，含 openclaw.extensions 入口
├── src/
│   ├── index.ts             # 插件入口：register(api) → registerContextEngine + registerTool
│   ├── bridge.ts            # ContextHubBridge：TS ContextEngine → HTTP 调用 sidecar
│   ├── tools.ts             # 7 个 MVP 工具定义（ls/read/grep/stat/store/promote/publish）
│   └── sidecar.py           # Python FastAPI 包装器：HTTP → ContextHubContextEngine → SDK
└── dist/                    # 编译输出的 JS
```

Bridge 采用**双进程架构**：TS bridge 运行在 OpenClaw 的 Node.js gateway 内，通过 HTTP 将 context engine 调用转发到 Python sidecar。Sidecar 托管实际的 `ContextHubContextEngine` 插件，使用 Python SDK 与 ContextHub server 通信。这一设计避免了在 Node.js 中嵌入 Python，同时保持了插件接口的清晰。
