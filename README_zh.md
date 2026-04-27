
<div align="center">

<img src="figures/logo2.jpeg" width="200">

### ContextHub: 面向多 Agent 协作的 <br> 统一上下文管理

基于**文件系统范式**和 **LLM 原生命令**的上下文治理引擎。
Agent 通过熟悉的文件操作（`ls`、`read`、`grep`、`stat`）经由 `ctx://` URI
导航和管理记忆、技能、文档和数据湖元数据——
具备版本控制、可见性边界、变更传播和跨 Agent 共享能力。

基于 FastAPI + PostgreSQL 构建。单数据库。无外部向量库。无消息队列。

[English](README.md) | 中文
</div>

---

## 为什么需要 ContextHub？ 🔎

当多个 AI Agent 协作处理相同的业务实体时，它们的上下文是孤立的、无版本的、互不连通的：

> * **41–87% 的多 Agent LLM 系统在生产中失败**——失败根因聚类为系统设计问题、Agent 间不一致和任务验证缺陷，属于系统架构层面的结构性缺陷，而非单个模型能力不足（[Cemri et al., NeurIPS 2025](https://arxiv.org/abs/2503.13657)）。

这是系统架构层面的结构性缺陷——无法通过提升单个模型的能力来解决。ContextHub 将四类上下文统一在一个治理层下来解决这一问题。

## ContextHub 管理什么？ 📦

| 上下文类型 | 含义 | 示例 |
|---|---|---|
| **Memory（记忆）** | Agent 在对话中学到的事实、模式和决策 | 一个在月度销售报表中验证有效的 SQL 查询模式 |
| **Skill（技能）** | Agent 发布、版本化并订阅的可复用能力 | "SQL 生成器"技能——订阅者在 breaking change 时收到通知 |
| **Resource（资源）** | Agent 阅读、理解和检索的文档 | Agent 在任务中引用的 API 文档、运维手册或规范文件 |
| **Data-Lake Metadata（数据湖元数据）** | 数据湖表的结构化元数据——表结构、字段、血缘关系 | 表 `orders(user_id, amount, created_at)` 及其上下游依赖关系 |

四者统一在 `ctx://` URI 命名空间下，共享相同的版本控制、可见性和变更传播语义。

> 各上下文类型的研究空白详细分析，请参阅 [Research Positioning](docs/research/research-positioning.md)。

## 核心能力 ✨

| 能力 | 解决什么问题 |
|---|---|
| **文件系统范式** | 所有上下文类型统一为 `ctx://` URI 下的文件——记忆、技能、文档、表元数据共用一套模型 |
| **LLM 原生命令** | Agent 使用 `ls`、`read`、`grep`、`stat` 操作上下文——LLM 天然理解文件操作，无需学习自定义 API |
| **多 Agent 协作** | 团队层级可见性继承（子读父、父不见子）；记忆晋升 `私有 → 团队 → 组织`，`derived_from` 血缘追踪 |
| **版本管理** | 将 Agent 锁定在稳定版本；`is_breaking` 标记防止静默破坏；已发布版本不可变 |
| **变更传播** | 上游变更自动通知所有下游依赖方——无需轮询，不是"最新版覆盖一切" |
| **L0/L1/L2 分层检索** | 向量检索 → BM25 精排 → 按需加载完整内容；相比平坦检索**节省 60–80% token** |
| **租户隔离** | 所有表启用行级安全（RLS）；请求级租户绑定 |
| **PostgreSQL 单库架构** | ACID + RLS + LISTEN/NOTIFY + pgvector 集于一库；无双写、无消息队列 |

## 架构 🏛️

```
         Agents（通过 OpenClaw Plugin / SDK 接入）
              │
              ▼
    ContextHub Server (FastAPI)
    ├── ContextStore       — ctx:// URI 路由
    ├── MemoryService      — 记忆晋升、血缘、团队共享
    ├── SkillService       — 发布、订阅、版本解析
    ├── RetrievalService   — pgvector + BM25 精排
    ├── PropagationEngine  — outbox、重试、依赖分发
    └── ACLService         — 可见性 / 写权限
              │
              ▼
    PostgreSQL + pgvector（单库：元数据 + 内容 + 向量 + 事件）
```

**单数据库。无外部向量库。无消息队列。** 消除双写一致性问题，最小化私有化部署的基础设施复杂度。

---

## 快速开始 🚀

### 前置条件

- **Python 3.12+**
- **PostgreSQL 16** + **pgvector** 扩展

### 第 1 步：安装 PostgreSQL + pgvector

<details>
<summary><strong>macOS (Homebrew)</strong></summary>

```bash
brew install postgresql@16
brew install pgvector
brew services start postgresql@16
```

</details>

<details>
<summary><strong>Linux (Ubuntu / Debian)</strong></summary>

```bash
# 添加 PostgreSQL APT 源
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list

sudo apt update
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo systemctl start postgresql
```

</details>

验证 PostgreSQL 已启动：

```bash
pg_isready
# 预期输出: "accepting connections"
```

### 第 2 步：创建数据库

```bash
# macOS (Homebrew): psql postgres
# Linux: sudo -u postgres psql
psql postgres
```

在 `psql` 中执行：

```sql
CREATE USER contexthub WITH PASSWORD 'contexthub' SUPERUSER;
CREATE DATABASE contexthub OWNER contexthub;
\c contexthub
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
\q
```

> 需要 `SUPERUSER` 权限，因为 schema 使用了 `FORCE ROW LEVEL SECURITY`。本地开发环境无安全问题。

### 第 3 步：安装并启动 ContextHub

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
pip install greenlet
pip install -e sdk/
```

配置 `.env`（从示例文件复制后，填入你的 API Key；如有需要再调整模型配置）：

```bash
cp .env.example .env
```

| 变量 | 是否必填 | 默认值 | 用途 |
|---|---|---|---|
| `API_KEY` | 是 | `changeme` | 服务端认证——所有 SDK/HTTP 请求需在 `X-API-Key` header 中携带此值 |
| `OPENAI_API_KEY` | 使用 LLM 相关功能时必填 | *（空）* | 启用 `search` 所需的向量 embedding，以及长文档入库 / 检索时用到的文档树构建与 tree 选段；不填则向量搜索不可用，长文档入库会返回 `503` |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | chat 和 embedding 共用的 base URL；也可以指向兼容 OpenAI API 的端点 |
| `CHAT_MODEL` | 否 | `gpt-4o-mini` | 长文档文档树构建与 tree-based section selection 使用的 chat model |
| `EMBEDDING_MODEL` | 否 | `text-embedding-3-small` | 检索阶段为 L0/query 生成 embedding 的模型 |
| `EMBEDDING_DIMENSIONS` | 否 | `1536` | 向量在库中的目标存储维度；与该维度一致时直接写入，更短的兼容 embedding 会自动补零，更长的 embedding 会被拒绝 |

默认情况下，ContextHub 使用官方 OpenAI 端点，chat model 为 `gpt-4o-mini`，embedding model 为 `text-embedding-3-small`。如果你想切换到其他兼容 OpenAI API 的部署，只需一起调整 `OPENAI_BASE_URL`、`CHAT_MODEL` 和 `EMBEDDING_MODEL`。如果新 provider 返回的 embedding 比配置的 `EMBEDDING_DIMENSIONS` 更短，ContextHub 会在写库和检索前自动在尾部补零，使其仍能适配当前 pgvector 列。

```bash
# 执行数据库迁移
alembic upgrade head

# 启动服务
uvicorn contexthub.main:app --port 8000
```

验证：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

API 文档：http://localhost:8000/docs

### 第 4 步：使用 Python SDK

```python
import asyncio
from contexthub_sdk import ContextHubClient

async def main():
    async with ContextHubClient(
        url="http://localhost:8000",
        api_key="changeme",
        account_id="acme",
        agent_id="query-agent",
    ) as client:
        # 存储私有记忆
        memory = await client.memory.add(
            content="SELECT date_trunc('month', created_at), SUM(amount) FROM orders GROUP BY 1",
            tags=["sql", "sales"],
        )

        # 晋升为团队共享知识
        promoted = await client.memory.promote(uri=memory.uri, target_team="engineering")

        # 语义检索所有可见上下文
        results = await client.search("monthly sales summary", top_k=5)

asyncio.run(main())
```

ContextHub 同时可作为 [OpenClaw](https://github.com/anthropics/openclaw) 等 Agent 框架的即插即用 context engine——上下文治理对 Agent 代码完全透明。详见下方[与 OpenClaw 集成](#与-openclaw-集成-)。

完整的端到端 demo 和集成测试，请参阅[本地部署与端到端验证指南](docs/setup/local-setup&end2end-verification-guide-zh.md)。

---

## 与 OpenClaw 集成 🦞

ContextHub 设计为 [OpenClaw](https://github.com/anthropics/openclaw) 的 **context engine**——替换内置引擎，提供企业级上下文治理。

```bash
# 一条命令安装
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

**自动行为（无需修改 Agent 代码）：**

| 事件 | ContextHub 行为 |
|------|----------------|
| Agent 收到用户提问 | `assemble()` — 检索所有可见上下文，将相关内容注入系统提示词 |
| Agent 完成回复 | `afterTurn()` — 提取可复用事实，存为私有记忆 |

**每个会话自动可用的 7 个 Agent 工具：**

`ls` · `read` · `grep` · `stat` · `contexthub_store` · `contexthub_promote` · `contexthub_skill_publish`

### 多 Agent 协作实战

```
组织：engineering/backend ← query-agent        组织：data/analytics ← analysis-agent
                                                     （同时也是 engineering 成员）
```

```
1. query-agent 将一个 SQL pattern 存为私有记忆

2. query-agent 晋升到 engineering 团队
   → ctx://team/engineering/shared_knowledge/monthly-sales-pattern

3. analysis-agent 提问："月度销售额应该怎么查？"
   → ContextHub 通过 assemble() 自动召回已晋升的 pattern
   → 零人工传递

4. query-agent 发布 breaking Skill v2
   → analysis-agent（pinned 到 v1）继续稳定使用 v1
   → advisory："v2 已发布，包含 breaking changes"
```

> **这和共享文档有什么不同？**
> ContextHub 强制执行可见性边界、追踪 `derived_from` 血缘、
> 沿依赖图传播变更——而不仅仅是"谁最后编辑的就是最新版"。

完整搭建流程请参考 [OpenClaw 集成指南](docs/setup/openclaw-integration-guide.md)。

---

## 路线图 🗺️

- [x] **Phase 1 — 核心功能** ✅
  - 基础框架：使用统一的 `ctx://` 地址体系覆盖所有上下文类型，包括记忆存储与共享、技能版本管理与订阅、语义检索、自动变更传播等核心服务。
  - 基于 PostgreSQL 行级安全的团队层级可见性与租户隔离。
  - Python SDK 与 OpenClaw 上下文引擎插件。
  - 17 项集成测试覆盖传播正确性、多 Agent 协作和访问隔离。
- [x] **Phase 2 — 细粒度访问控制与审计** ✅
  - 在默认团队可见性之上增加显式权限规则：
    - 管理员可禁止特定资源的访问（禁止规则始终优先于允许规则）；
    - 对敏感字段自动脱敏（如薪资 → `[MASKED]`）；
    - 授权特定 Agent 访问其他团队的上下文。
  - 操作审计日志（关键操作事务级保证不丢失）与管理 API，支持按资源、Agent、时间段查询访问记录。
  - 11 项新增集成测试。
- [x] **Phase 3 — 反馈与生命周期** ✅
  - 质量反馈闭环：追踪 Agent 是否实际使用了检索到的上下文、忽略了它、还是被用户纠正。
  - 自动内容生命周期管理（长期未使用的上下文逐步降权：活跃 → 过时 → 归档）。
  - 面向长文档（百页以上财报、技术手册）的树导航检索策略。
- [ ] **Phase 4 — 三层量化 Benchmark**
  - 基于公开基准数据集和自建数据集，在三个领域进行系统性量化评估。
    - **第一层**（核心指标）：上下文检索质量——长文档检索（FinanceBench）、记忆召回（LoCoMo）、混合上下文检索（自建）。
    - **第二层**：ContextHub 独有的协作治理指标——变更传播精度、跨 Agent 知识迁移效果。
    - **第三层**（辅助验证）：以下游任务准确率（SQL 生成、文档问答）作为间接证据。
- [ ] **Phase 5 — 生产加固**
  - 多实例部署：变更传播引擎可多节点并发处理事件（基于 PostgreSQL `SKIP LOCKED`）；
  - MCP Server 以接入更广泛的 Agent 框架；
  - 对接企业数据目录（如 Hive Metastore），替换当前的模拟连接器。

## 文档 📄

| 文档 | 说明 |
|------|------|
| [OpenClaw 集成指南](docs/setup/openclaw-integration-guide.md) | 将 ContextHub 作为 OpenClaw context engine 的完整 5 终端搭建 |
| [本地部署与端到端验证](docs/setup/local-setup&end2end-verification-guide-zh.md) | 开发环境搭建、数据库迁移、端到端 demo |
| [MVP 验证计划](docs/mvp%20verification/mvp-verification-plan.md) | 三层验证：自动化测试 → API 闭环 → 运行时合同 |
| [开发者指南](docs/design%20and%20development/development-guide-zh.md) | API 概览、SDK 参考、技术选型、项目结构 |

## 参考文献 📚

- [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657) — Cemri et al., NeurIPS 2025：7 个 MAS 框架中 14 种失败模式的 MAST 分类法
- [Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026
- [Governed Memory](https://arxiv.org/abs/2603.17787) — Taheri, 2026
- [Collaborative Memory](https://arxiv.org/abs/2505.18279) — 多用户记忆共享 + 动态 ACL
- [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源（个人版上下文管理）
- [Model Context Protocol](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024

## 许可证 ⚖️

[Apache License 2.0](LICENSE)
