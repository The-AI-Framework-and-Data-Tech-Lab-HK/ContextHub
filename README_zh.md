# ContextHub

**面向企业多 Agent 协作的统一上下文治理中间件。**

[English](README.md) | 中文

## 问题：从记忆管理到上下文治理

当多个 AI Agent 在企业环境中协作操作同一组业务实体时，各 Agent 的上下文——记忆、技能、策略文档、Schema——分散存储、缺乏版本控制、彼此断联。研究表明 **79% 的多 Agent 系统失败源于协调问题而非技术 bug**（[Zylos Research, 2026](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical)），**36.9% 的多 Agent 失败来自 inter-agent misalignment**——Agent 忽略、重复或矛盾彼此的工作（[Cemri et al., 2025](https://arxiv.org/abs/2503.13657)）。这类失败无法通过提升单个 Agent 的模型能力来解决，其根因在于系统架构层面的结构性缺陷。

现有框架将"Agent 上下文管理"几乎等同于**记忆管理**。Governed Memory、Collaborative Memory、MemOS 均以记忆为核心抽象。但企业 Agent 系统实际需要治理的上下文远不止记忆一种：

| 上下文类型 | 典型内容 | 文献覆盖程度 |
|---|---|---|
| **Memory** | 对话记忆、实体状态、工作记忆 | 相对最多，但多用户协作版本管理仍稀缺 |
| **Skill** | 工具定义、Prompt 模板、Agent 配置 | **几乎空白**——无 breaking change 检测 + 订阅者通知的端到端生命周期 |
| **Resource（RAG 文档）** | 策略文档、合规规则、知识库 | 仅覆盖"检索最新版本"，不覆盖"沿依赖图传播变更" |
| **结构化元数据** | 数据库 Schema、数据湖 Catalog | 无 AI Agent 语境下的研究 |

ContextHub 填补了这一空白。据我们所知，对 Memory、Skill、Resource 和结构化元数据的**统一版本治理**，作为一个端到端问题，在现有文献中尚无系统性研究。

## 设计贡献

ContextHub 是面向 toB 多 Agent 协作的上下文治理中间件，提供**统一的上下文状态层**——涵盖共享记忆、可见性边界、版本治理和变更传播。

| 贡献 | 解决什么问题 | 为什么是新的 |
|---|---|---|
| **Skill 版本管理 + breaking change 传播** | 发布者标记 `is_breaking` → 订阅者收到 `stale` / `advisory` 通知 → pinned 订阅者保持稳定 | 现有 AI Agent 框架无一处理完整生命周期：发布 → breaking 标记 → 订阅者通知 → stale 标记 → 恢复 |
| **依赖图驱动的变更传播** | 上游策略/Schema 变更时，自动通知或更新所有依赖它的下游 Agent | Temporal/Corrective RAG 解决"检索当前文档"，不解决"谁依赖了这个文档、需要被通知" |
| **层级式团队所有权 + 可见性继承** | 子团队可见父团队内容；父团队默认不可见子团队私有内容 | 超越 Mem0 的平坦 user/agent/app 隔离，支持企业组织结构 |
| **L0/L1/L2 分层检索模型** | 一句话摘要（L0，向量检索）→ 结构化概览（L1，精排）→ 完整内容（L2，按需加载） | 相比全量 Schema dump，上下文 token 消耗降低 60-80% |
| **PostgreSQL 中心单库架构** | ACID 事务、RLS 租户隔离、LISTEN/NOTIFY 变更传播、递归 CTE 血缘查询、pgvector 语义检索——全部在一个数据库中 | 消除独立向量库、消息队列、元数据库之间的双写一致性问题 |

### 与现有方案的差异

| 框架 | 局限 | ContextHub 的解法 |
|---|---|---|
| **Mem0** | 平坦 user/agent/app 隔离；无团队层级、无变更传播、无版本管理；仅 SaaS | 层级团队 + 传播 + 版本 + 可私有化部署 |
| **CrewAI / LangGraph** | 记忆系统面向单一框架内协调，无法跨框架、跨团队、跨时间管理组织级知识 | 框架无关的中间件，通过 SDK + 插件对接 |
| **OpenAI Agents SDK** | 无内置记忆、无 ACL、无租户隔离 | 完整治理层 |
| **Governed Memory (Personize.ai)** | 最接近，但聚焦 CRM 实体（contacts/companies/deals），非通用 Agent 上下文管理 | 通用 `ctx://` URI 抽象，支持任意上下文类型 |
| **OpenViking** | 核心上下文管理理念（一切皆文件 + 记忆管线 + 向量检索），但定位个人版——不支持多 Agent 隔离、团队层级、ACL、变更传播 | 继承 OpenViking 的 URI + L0/L1/L2 抽象，扩展至企业多租户架构 |

## 架构

```
         Agents（通过 OpenClaw Plugin / SDK 接入）
              │
              ▼
    ContextHub Server (FastAPI)
    ├── ContextStore       — ctx:// URI 路由（read/write/ls/stat）
    ├── MemoryService      — 记忆晋升、derived_from、团队共享
    ├── SkillService       — 发布、订阅、版本解析
    ├── RetrievalService   — 统一检索（pgvector + BM25 精排）
    ├── PropagationEngine  — outbox 消费、重试、依赖/订阅分发
    └── ACLService         — 默认可见性 / 写权限
              │
              ▼
    PostgreSQL + pgvector
    （元数据、内容、向量、事件 — 全部在一个数据库中）
```

**单数据库。无外部向量库。无消息队列。** PostgreSQL 原生提供 ACID 事务、RLS 租户隔离、LISTEN/NOTIFY 变更传播、递归 CTE 血缘查询，以及 pgvector 语义检索。这一设计选择消除了双写一致性问题，最小化了企业私有化部署的基础设施复杂度。

### 设计原则

- **URI 是逻辑地址，不是物理路径。** `ctx://datalake/prod/orders` 对应 PostgreSQL 中的一行，而非磁盘上的文件。Agent 感知到文件语义，系统提供数据库保证。
- **元数据和内容同库。** L0/L1/L2 内容存在 PostgreSQL TEXT 列中（TOAST 自动处理大文本），与元数据在同一事务中原子更新。
- **只有 L0 被向量化。** L0 摘要（~100 tokens）用于语义检索。L1/L2 通过 URI 从同一张表读取——无跨系统开销。

## 核心能力

### 多 Agent 协作
- **团队所有权模型**：层级式可见性继承（子团队可读父团队；父团队默认不可见子团队）
- **记忆晋升**：`私有 → 团队 → 组织`，`derived_from` 追踪来源血缘
- **跨 Agent 知识复用**：晋升后的记忆可被团队成员检索和使用

### Skill 版本管理
- 发布新版本时标记 `is_breaking`
- 订阅者选择 `pinned`（锁定版本）或 `latest`（浮动跟踪）解析策略
- Breaking change 自动将下游依赖方标记为 `stale`，并附带 advisory 通知
- 已发布版本不可变；URI 始终指向最新 published（pin 是视角，不是新地址）

### 变更传播
- 三级传播规则：纯规则（70%，零 token） / 模板替换（20%） / LLM 推理（10%）
- Outbox 模式，`change_events` 表为唯一事实源
- NOTIFY 快速唤醒 + 周期补扫保证最终送达
- 指数退避自动重试；crash 后通过 lease 超时恢复
- 幂等副作用：`mark_stale`、`auto_update`、`notify`、`advisory`

### L0/L1/L2 分层检索
- **L0**：一句话摘要 + embedding（pgvector 向量检索）
- **L1**：结构化概览（BM25 关键词精排）
- **L2**：完整内容（按需加载）
- 优雅降级：embedding 服务不可用时，自动回退到关键词检索

### 可见性与租户隔离
- 所有面向 Agent 的表启用行级安全策略（RLS）
- `SET LOCAL app.account_id` 通过 request-scoped `ScopedRepo` 限定在每个事务内
- 默认可见性基于团队层级 + scope 规则；显式 ACL 作为 post-MVP 叠加层

## 快速开始

### 前置条件

- Python 3.12+
- Docker & Docker Compose
- PostgreSQL 16 + pgvector（通过 docker-compose 提供）

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

### 3. 执行数据库迁移

```bash
alembic upgrade head
```

### 4. 启动服务

```bash
uvicorn contexthub.main:app --reload
```

API 地址：`http://localhost:8000`，OpenAPI 文档：`/docs`。

### 5. 使用 SDK

```python
from contexthub_sdk import ContextHubClient

client = ContextHubClient(base_url="http://localhost:8000", api_key="...")

# 语义检索所有可见上下文
results = await client.search("月度销售额统计", scope=["datalake"], top_k=5)

# 记录成功案例为私有记忆
memory = await client.add_memory(content="SELECT ... GROUP BY month", tags=["sql", "sales"])

# 晋升为团队共享记忆
promoted = await client.promote_memory(uri=memory.uri, target_team="engineering/backend")

# 发布 Skill 新版本
version = await client.publish_skill_version(
    skill_uri="ctx://team/engineering/skills/sql-generator",
    content="...",
    changelog="新增 window function 支持",
    is_breaking=True,
)
```

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
├── alembic/              # 数据库迁移
├── tests/                # 集成测试（可见性、传播、检索等）
├── plan/                 # 设计文档（15 篇，从不变式到实施计划）
└── docs/                 # 部署指南、验证计划、集成指南
```

## 路线图

- [x] **Phase 1 — MVP 核心**（已完成）
  - 项目脚手架、Docker、PostgreSQL + pgvector 初始化
  - 核心表 + RLS + 触发器 + 种子数据
  - Request-scoped 数据库执行模型（`PgRepository` / `ScopedRepo`）
  - ACLService（默认可见性 / 写权限，递归 CTE 团队层级展开）
  - ContextStore（`ctx://` URI 路由：read/write/ls/stat）
  - MemoryService（添加、列表、晋升 + `derived_from` 血缘）
  - SkillService（发布、订阅、pinned/latest/显式版本解析）
  - RetrievalService（pgvector 检索 + BM25 精排 + ACL 过滤 + 优雅降级）
  - PropagationEngine（outbox 消费、三级规则、重试/恢复、NOTIFY + 补扫）
  - Python SDK + OpenClaw context-engine 插件
  - 数据湖载体（MockCatalogConnector、CatalogSyncService、sql-context 组装）
  - Tier 3 集成测试（传播 P-1~P-8、协作 C-1~C-5、可见性 A-1~A-4）
- [ ] **Phase 2 — 显式 ACL 与审计**
  - 显式 ACL allow/deny/field mask 叠加层
  - 审计日志（append-only `audit_log` 表）
  - "reference + ACL" 窄范围跨团队共享
- [ ] **Phase 3 — 反馈与生命周期**
  - 反馈闭环（adopted/ignored 信号、质量评分）
  - 生命周期管理（自动 stale → archived → deleted 转换）
  - 长文档检索扩展
- [ ] **Phase 4 — 量化评估（ECMB）**
  - Tier 1 基准测试：SQL 执行准确率、表检索精确率/召回率、每次查询 Token 消耗
  - Tier 2 A/B 实验：L0/L1/L2 vs 平坦 RAG、有/无结构化关系、有/无传播
- [ ] **Phase 5 — 生产加固**
  - 多实例部署（`SELECT FOR UPDATE SKIP LOCKED`）
  - MCP Server 集成
  - 真实 Catalog 连接器（Hive/Iceberg/Delta）
  - 运行快照 / 上下文打包

## 设计文档

`plan/` 目录包含 15 篇设计文档，覆盖完整系统设计：

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

## 参考文献

- [AI Agent Memory Architectures for Multi-Agent Systems](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical) — Zylos Research, 2026
- [How to Design Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026
- [Governed Memory: A Production Architecture for Multi-Agent Workflows](https://arxiv.org/abs/2603.17787) — Taheri, 2026
- [Collaborative Memory: Multi-User Memory Sharing with Dynamic Access Control](https://arxiv.org/abs/2505.18279)
- [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源（个人版上下文管理）
- [Model Context Protocol (MCP)](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024

## 许可证

[Apache License 2.0](LICENSE)
