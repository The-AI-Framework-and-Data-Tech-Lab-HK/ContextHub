# 08 — 系统架构

## 架构图

```
┌─────────────────────────────────────────────────────────┐
│              OpenClaw（单实例，多 Agent 身份）              │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐      │
│  │ 数据查询  │  │ 数据分析  │  │ 其他业务 Agent   │      │
│  │ agent_id │  │ agent_id │  │ agent_id         │      │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘      │
│       └──────────────┼────────────────┘                 │
│                      ▼                                   │
│         ContextHub OpenClaw Plugin                       │
│   （注册 tools + lifecycle hooks，调用 SDK）              │
│                      ▼                                   │
│              ContextHub Python SDK                       │
│          （ctx:// URI + 文件语义接口）                     │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│                  ContextHub Server (FastAPI)              │
│                                                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────────┐     │
│  │ Context    │  │ Memory     │  │ Skill          │     │
│  │ Service    │  │ Service    │  │ Service         │     │
│  └─────┬──────┘  └─────┬──────┘  └──────┬─────────┘     │
│        │               │                │               │
│  ┌─────┴───────────────┴────────────────┴──────────┐    │
│  │              Core Engine                         │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐    │    │
│  │  │ Retrieval│ │ Indexer  │ │ Propagation  │    │    │
│  │  │ Engine   │ │          │ │ Engine       │    │    │
│  │  └──────────┘ └──────────┘ └──────────────┘    │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐    │    │
│  │  │ Auth &   │ │ Audit    │ │ Version      │    │    │
│  │  │ ACL      │ │ Logger   │ │ Manager      │    │    │
│  │  └──────────┘ └──────────┘ └──────────────┘    │    │
│  │  ┌──────────┐ ┌──────────────┐                  │    │
│  │  │ Feedback │ │ Lifecycle    │                  │    │
│  │  │ Collector│ │ Manager      │                  │    │
│  │  └──────────┘ └──────────────┘                  │    │
│  └─────────────────────┬───────────────────────────┘    │
│                        ▼                                 │
│  ┌──────────────────────────────────────────────────┐   │
│  │           ContextStore（URI 路由层）               │   │
│  │   ctx:// URI → PG 读写 + 向量检索 + ACL 检查     │   │
│  └──────────────────────┬────────────────────────────┘   │
└──────────────────────────┼───────────────────────────────┘
                           ▼
┌──────────────────────────────────┐  ┌──────────────┐
│   PostgreSQL + pgvector          │  │ Catalog      │
│                                  │  │ Connector    │
│ ┌──────────────┐ ┌─────────────┐ │  │              │
│ │ contexts     │ │ pgvector    │ │  │ Hive/Iceberg/│
│ │ dependencies │ │             │ │  │ Delta/Mock   │
│ │ change_events│ │ L0 embedding│ │  │              │
│ │ table_meta.. │ │ HNSW 索引   │ │  │              │
│ │ lineage      │ │             │ │  │              │
│ │ table_rels.. │ └─────────────┘ │  │              │
│ │ query_templ..│                  │  │              │
│ │ skill_vers.. │ PG 原生能力：    │  │              │
│ │ access_pol.. │ LISTEN/NOTIFY   │  │              │
│ │ audit_log    │ RLS (租户隔离)   │  │              │
│ │ team_member..│ ACID 事务        │  │              │
│ │ lifecycle_.. │ 递归 CTE (血缘)  │  │              │
│ │ context_fb.. │                  │  │              │
│ └──────────────┘                  │  │              │
└──────────────────────────────────┘  └──────────────┘
```

## 与原方案的关键区别

| 维度 | 原方案（三层存储抽象） | 新方案（PG 中心） |
|------|----------------------|-------------------|
| 内容存储 | ContentStore 接口（S3/LocalFS） | PG TEXT 列（TOAST 自动处理） |
| 元数据存储 | 文件名/路径/向量 DB 标量字段 | PG 结构化表 |
| 关系存储 | `.relations.json` / `.deps.json` 文件 | PG `dependencies` / `table_relationships` 表 |
| 事件系统 | append-only JSON / Redis Streams | PG `change_events` 表 + `LISTEN/NOTIFY` |
| 权限 | 应用层路径前缀检查 | PG RLS + `access_policies` 表 |
| 审计 | append-only JSON 文件 | PG `audit_log` 表（事务内写入） |
| 事务保证 | 无（内容和元数据可能不一致） | ACID（内容、元数据、事件在同一事务中） |
| 向量索引 | 独立向量 DB（Chroma/Milvus），需双写对账 | pgvector 扩展，与元数据同库同事务 |

## DataAgent 层说明

MVP 阶段采用单 OpenClaw 实例作为 DataAgent 运行时。ContextHub OpenClaw Plugin 注册为 **context-engine 插件**（占据 `plugins.slots.contextEngine` 槽位），采用增强型适配器模式（参考 OpenViking 新版 openclaw-plugin 的 context-engine 架构，详见 13-related-works.md）：

- Plugin 声明 `kind: "context-engine"`，通过 `api.registerContextEngine()` 注册
- Plugin 注册 ContextTools（`ls`、`read`、`grep`、`stat`）和业务工具（`contexthub_store`、`contexthub_promote` 等）
- 在 ContextEngine 的 `assemble` 方法中，通过 `systemPromptAddition` 注入来自 PG 的相关上下文（auto-recall），不修改 messages 数组
- 在 ContextEngine 的 `afterTurn` 方法中，提取记忆写入 PG（auto-capture）
- 在 ContextEngine 的 `compact` 方法中，委托给 OpenClaw 内置 LegacyContextEngine（不声明 `ownsCompaction`）
- 多 Agent 协作通过 SDK 调用时切换 `agent_id` 参数实现，ContextHub Server 端按 `agent_id` 做隔离和协作
- 协作逻辑（传播、ACL、记忆晋升）全部在 Server 端闭环，不依赖多个 Agent 运行时实例

### OpenClaw 插件架构决策

基于对 lossless-claw 和 OpenViking 两个 OpenClaw context-engine 插件的分析（详见 13-related-works.md），ContextHub 的 OpenClaw 插件采用以下架构：

**1. 增强型适配器，而非完整 ContextEngine**

ContextHub 的核心价值是企业上下文管理（数据湖元数据、团队记忆、Skills、权限治理），不是对话历史压缩。因此：
- `assemble`：透传 messages（不修改对话历史），通过 `systemPromptAddition` 注入 PG 上下文
- `compact`：委托给 OpenClaw 内置引擎或 lossless-claw（不声明 `ownsCompaction`）
- `afterTurn`：唯一的主动逻辑——提取记忆写入 PG
- `ingest` / `ingestBatch`：空操作

**2. 上下文注入通道选择**

使用 `AssembleResult.systemPromptAddition` 而非修改 messages 数组。原因：
- PG 上下文（auto-recall 结果、数据湖元数据）应每轮动态生成，不应进入对话历史
- 如果注入到 messages 中，compaction 引擎会将其当作普通对话消息压缩，导致上下文失真
- `systemPromptAddition` 进入系统提示，与对话历史隔离，compaction 不会触碰

**3. Compaction 委托**

ContextHub 不管理对话历史压缩。compact 方法内部尝试动态 import OpenClaw 的 LegacyContextEngine 进行委托（与 OpenViking 新版的 `tryLegacyCompact()` 模式一致）。如果未来需要更高质量的压缩，可选择将 lossless-claw 的 compaction 算法作为内部依赖集成。

**4. 与 lossless-claw 的共存**

默认场景不需要共存——ContextHub 占据 contextEngine slot，compact 委托给 Legacy 引擎。如果用户同时需要 lossless-claw 的 DAG 无损压缩能力，采用方案 A：ContextHub 内部集成 lossless-claw 的压缩组件作为依赖，在 compact 方法中调用其 compaction 算法替代 Legacy 引擎。

## 核心模块职责

| 模块 | 职责 | 依赖的 PG 表 |
|------|------|-------------|
| Context Service | 上下文 CRUD、L0/L1/L2 管理 | `contexts` |
| Memory Service | 记忆提取、去重、热度管理、共享/提升 | `contexts`, `dependencies` |
| Skill Service | Skill 定义、版本管理、发布/订阅 | `contexts`, `skill_versions`, `dependencies`（dep_type='skill_subscription'） |
| Retrieval Engine | pgvector 检索 L0 → PG 读 L1 精排 → 按需加载 L2 | `contexts`（含 pgvector 索引） |
| Indexer | 内容变更时异步生成 L0/L1、更新 pgvector embedding | `contexts` |
| Propagation Engine | 监听 NOTIFY → 查依赖方 → 执行规则 | `change_events`, `dependencies` |
| Auth & ACL | 认证、RBAC、资源级权限、字段脱敏 | `access_policies`, `team_memberships` |
| Audit Logger | 操作审计、上下文溯源 | `audit_log` |
| Version Manager | Skill 版本管理 | `skill_versions` |
| Feedback Collector | 隐式反馈采集、质量评分计算 | `context_feedback`, `contexts` |
| Lifecycle Manager | 状态机、定期归档/清理、湖表同步删除 | `contexts`, `lifecycle_policies` |
| ContextStore | URI 路由层：ctx:// → PG 读写 + ACL 检查 | 所有表 |
| CatalogConnector | 数据目录抽象（Hive/Iceberg/Delta/Mock） | `table_metadata`, `lineage` |

## 数据流

### 写入流程

```
Agent 写入 ctx://agent/bot/memories/cases/sql-001
    │
    ▼
ContextStore.write()
    │
    ├─ 1. ACL 检查（查 access_policies 表）
    │
    ├─ 2. PG 事务 {
    │      INSERT/UPDATE contexts 表
    │      INSERT dependencies 表（自动注册依赖）
    │      INSERT change_events 表
    │      INSERT audit_log 表
    │  }
    │
    ├─ 3. PG NOTIFY 'context_changed'
    │
    └─ 4. 异步：Indexer 生成 L0 embedding → 写入 PG pgvector 列
```

### 检索流程

```
Agent 搜索 "月度销售额统计"
    │
    ▼
ContextStore.search()
    │
    ├─ 1. pgvector：L0 embedding 语义匹配 + 标量过滤 → top-K URI
    │
    ├─ 2. PG：批量读取 L1 内容 → Rerank
    │
    ├─ 3. ACL 过滤 + 字段脱敏
    │
    └─ 4. 返回结果（按需可继续加载 L2）
```

### 变更传播流程

```
CatalogConnector 检测到 orders 表 schema 变更
    │
    ▼
PG 事务 {
    UPDATE table_metadata SET ddl = ...
    UPDATE contexts SET version = version + 1
    INSERT change_events
}
    │
    ▼
PG NOTIFY 'context_changed'
    │
    ▼
Propagation Engine 被唤醒
    │
    ├─ SELECT FROM dependencies WHERE target_uri = 'ctx://datalake/.../orders'
    │
    ├─ 对每个依赖方执行 PropagationRule
    │     └─ TableSchemaRule → auto_update（重新生成 L0/L1）
    │     └─ SkillVersionRule → mark_stale（如果是 breaking）
    │
    └─ UPDATE contexts SET status = 'stale' WHERE uri IN (...)
```
