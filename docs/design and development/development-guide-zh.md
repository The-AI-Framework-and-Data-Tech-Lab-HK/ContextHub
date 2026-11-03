# ContextHub — 开发者指南

> API 参考、技术选型、SDK 使用和项目结构，面向 ContextHub 贡献者。
>
> 本地开发环境搭建请参考 [本地部署与端到端验证指南](../setup/local-setup&end2end-verification-guide-zh.md)。
> OpenClaw 集成请参考 [OpenClaw 集成指南](../setup/openclaw-integration-guide-zh.md)。
> Deep Agents 接入请参考[集成教程](../setup/deepagents-integration-guide.md)和[适配器参考](../../plugins/deepagents/README.md)。

## 快速开始

### 前置条件

- Python 3.12+
- Docker & Docker Compose（或 macOS 上通过 Homebrew 安装的 PostgreSQL）
- PostgreSQL 16 + pgvector

### 1. 克隆并安装

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
pip install -e ".[dev]" -e ./sdk greenlet
```

### 2. 启动 PostgreSQL

```bash
docker compose up -d
```

启动 PostgreSQL 16 + pgvector，端口 5432（用户：`contexthub`，密码：`contexthub`，数据库：`contexthub`）。

macOS 用户如不使用 Docker，请参考 [本地部署指南](../setup/local-setup&end2end-verification-guide-zh.md) 中的 Homebrew 安装方式。

### 3. 执行数据库迁移

```bash
alembic upgrade head
```

### 4. 启动服务

```bash
uvicorn contexthub.main:app --reload
```

API 地址：`http://localhost:8000`，OpenAPI 文档：`/docs`。

## Python SDK

两种 Agent 适配器都使用同一套异步 SDK，也可以直接调用 SDK。创建客户端时绑定账号和 Agent 身份，调用期间保持客户端打开：

```python
import asyncio
from contexthub_sdk import ContextHubClient, Scope

async def main():
    async with ContextHubClient(
        url="http://localhost:8000",
        api_key="changeme",
        account_id="acme",
        agent_id="query-agent",
    ) as client:
        memory = await client.memory.add(
            content="比较月度销售额之前，先按月份汇总销售数据。",
            tags=["sql", "sales"],
        )
        await client.memory.promote(uri=memory.uri, target_team="engineering")
        results = await client.search(
            "月度销售额统计", scope=[Scope.TEAM], top_k=5,
        )
        print(results.model_dump_json(indent=2))

asyncio.run(main())
```

示例使用数据库预置的 `acme` 账号和 `query-agent`；请将 `api_key` 改为运行中服务端的 `API_KEY`。更新已有技能版本使用 `client.skill.publish(skill_uri=..., content=..., changelog=..., is_breaking=...)`。上传文档和读取章节使用 `client.document` 下的方法。

## Agent 接入方式

| 项目 | OpenClaw | Deep Agents |
|---|---|---|
| 调用链 | 上下文引擎插件 → TypeScript bridge → Python sidecar → SDK | 原生异步工具 → SDK |
| ContextHub 工具 | 8 个工具 | 使用相同的 8 个操作名称，另有 `contexthub_sections` 和 `contexthub_read_section` |
| 检索和保存 | 回合前后自动调用，也支持 Agent 主动调用工具 | Agent 主动调用工具，没有自动召回和自动保存机制 |
| 对话状态 | OpenClaw 管理历史和上下文压缩 | Deep Agents 管理对话状态和自身文件系统 |
| 搭建说明 | [OpenClaw 指南](../setup/openclaw-integration-guide-zh.md) | [Deep Agents 教程](../setup/deepagents-integration-guide.md) |

两边共有的工具名是 `contexthub_ls`、`contexthub_read`、`contexthub_search`、`contexthub_stat`、`contexthub_store`、`contexthub_promote`、`contexthub_skill_publish` 和 `contexthub_feedback`。具体参数和返回格式由各自适配器定义，名称一致不代表参数完全相同。OpenClaw 不再接受没有前缀的旧工具名；更新后需要重新构建 bridge、重启 OpenClaw 和 sidecar，并修改自定义提示词或工具白名单中的名称。

Deep Agents 适配器可通过 `python -m pip install -e ./sdk -e ./plugins/deepagents` 安装，另需安装所选模型对应的 LangChain 接入包。将 `create_contexthub_tools(client)` 交给 `create_deep_agent`，在客户端打开期间使用 `ainvoke` 或 `astream`。ContextHub 的 Agent 身份绑定在客户端上，切换对话线程不会切换该身份。

## API 概览

下表列出的是服务端 HTTP 路径。OpenClaw 和 Deep Agents 向模型提供的工具名使用
`contexthub_` 前缀，例如 `contexthub_read` 调用 `/api/v1/tools/read`，
`contexthub_search` 调用 `/api/v1/search`。

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
| POST | `/api/v1/tools/{ls,read,grep,stat}` | 文件式操作的 HTTP 端点，路径名称不是 Agent 工具名 |
| POST | `/api/v1/documents/ingest` | 上传文档并生成章节 |
| GET | `/api/v1/documents/{context_id}/sections` | 列出文档章节 |
| GET | `/api/v1/documents/{context_id}/section/{section_id}` | 读取章节原文 |
| POST | `/api/v1/feedback` | 反馈检索结果是否有用 |

## 与现有方案的差异

| 框架 | 局限 | ContextHub 的解法 |
|---|---|---|
| **Mem0** | 平坦 user/agent/app 隔离；无团队层级、无变更传播、无版本管理；仅 SaaS | 层级团队 + 传播 + 版本 + 可私有化部署 |
| **CrewAI / LangGraph** | 记忆系统面向单一框架内协调，无法跨框架、跨团队、跨时间管理组织级知识 | 框架无关的中间件，通过 SDK + 插件对接 |
| **OpenAI Agents SDK** | 无内置记忆、无 ACL、无租户隔离 | 完整治理层 |
| **Governed Memory (Personize.ai)** | 最接近，但聚焦 CRM 实体（contacts/companies/deals），非通用 Agent 上下文管理 | 通用 `ctx://` URI 抽象，支持任意上下文类型 |
| **OpenViking** | 核心上下文管理理念（一切皆文件 + 记忆管线 + 向量检索），但定位个人版——不支持多 Agent 隔离、团队层级、ACL、变更传播 | 继承 OpenViking 的 URI + L0/L1/L2 抽象，扩展至企业多租户架构 |

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

## 设计原则

- **URI 是逻辑地址，不是物理路径。** `ctx://datalake/prod/orders` 对应 PostgreSQL 中的一行，而非磁盘上的文件。Agent 感知到文件语义，系统提供数据库保证。
- **上下文记录存入 PostgreSQL。** 普通上下文的 L0/L1/L2 内容和元数据同库。长文档的原文件和提取文本存放在 `DOC_STORE_ROOT`，章节元数据和字符范围存入 PostgreSQL；见[章节生成说明](../setup/document-section-generation.md)。
- **只有 L0 被向量化。** L0 摘要（~100 tokens）用于语义检索。需要完整上下文时按 URI 读取，需要文档中的具体原文时调用章节接口。

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
├── plugins/deepagents/   # 原生异步工具、示例和适配器测试
├── bridge/               # TS bridge + Python sidecar（OpenClaw ↔ ContextHub）
├── alembic/              # 数据库迁移
├── tests/                # 单元测试和数据库集成测试
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
│   ├── tools.ts             # 8 个 contexthub_ 工具定义
│   └── sidecar.py           # Python FastAPI 包装器：HTTP → ContextHubContextEngine → SDK
└── dist/                    # 编译输出的 JS
```

Bridge 采用**双进程架构**：TS bridge 运行在 OpenClaw 的 Node.js gateway 内，通过 HTTP 将 context engine 调用转发到 Python sidecar。Sidecar 托管实际的 `ContextHubContextEngine` 插件，使用 Python SDK 与 ContextHub server 通信。这一设计避免了在 Node.js 中嵌入 Python，同时保持了插件接口的清晰。

## 验证改动

```bash
python -m pytest tests/test_sdk.py tests/test_plugin.py
cd bridge
npm ci
npm test
cd ..

# 修改 Deep Agents 适配器时，单独安装并运行它的测试
python -m pip install -e './plugins/deepagents[dev]'
python -m pytest plugins/deepagents/tests
```

默认跳过依赖数据库的测试。只有准备好可清空的测试数据库后，才设置 `CONTEXTHUB_INTEGRATION=1`；当前数据库 fixture 固定连接本机 5432 端口上的 `contexthub` 数据库，并会清空业务表。不要对已有研究数据库运行。Deep Agents 的测试目录不在根目录默认收集范围内，需显式指定。

## 设计文档

部分开发环境还保留了本地 `plan/` 设计笔记；该目录被 Git 忽略，不随仓库分发，也不是运行项目的必要条件。下表是这些笔记的主题索引：

| 文档 | 主题 |
|------|------|
| `00a-canonical-invariants` | 权威约束：租户唯一性、类型系统、可见性规则、状态机、版本不可变性 |
| `01-storage-paradigm` | 统一存储：URI 路由、PG 核心表、pgvector、可见性 SQL |
| `02-information-model` | L0/L1/L2 三层模型、记忆分类、热度评分 |
| `03-datalake-management` | 数据湖表管理：L2 结构化子表、CatalogConnector、Text-to-SQL 上下文组装 |
| `04-multi-agent-collaboration` | 团队所有权、Skill 版本管理、记忆晋升 |
| `05-access-control-audit` | 两层访问模型（默认 + 显式 ACL）、字段脱敏 |
| `06-change-propagation` | 事件驱动传播：outbox、三级规则、重试 |
| `07-feedback-lifecycle` | 反馈闭环、质量信号、生命周期治理 |
| `08-architecture` | 系统架构、模块职责、数据流 |
| `09-implementation-plan` | MVP 定义、验证矩阵、技术选型 |

