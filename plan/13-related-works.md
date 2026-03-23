# 13 — Related Works: OpenClaw 上下文引擎生态分析

ContextHub 以 OpenClaw plugin 形式对接 DataAgent。在设计 OpenClaw 插件之前，分析现有两个 OpenClaw 上下文管理插件的架构、接口和侧重点，作为设计参考。

---

## OpenClaw 插件槽位机制

OpenClaw 的插件体系通过 `plugins.slots` 声明不同类型的插件槽位。与上下文管理相关的有两个：

| 槽位 | 接口 | 职责 |
|------|------|------|
| `contextEngine` | `ContextEngine`（来自 `openclaw/plugin-sdk`） | 管理每轮对话的上下文窗口：消息持久化、上下文组装、压缩 |
| `memory` | Memory plugin hooks | 跨会话长期记忆的提取与召回（旧版接口） |

`contextEngine` 是单选 slot——同一时间只能有一个 ContextEngine 生效。

### ContextEngine 接口

来自 `openclaw/plugin-sdk`，核心方法：

```typescript
type ContextEngine = {
  info: ContextEngineInfo;
  bootstrap?: (params) => Promise<BootstrapResult>;
  ingest: (params: { sessionId; message; isHeartbeat? }) => Promise<IngestResult>;
  ingestBatch?: (params: { sessionId; messages; isHeartbeat? }) => Promise<IngestBatchResult>;
  assemble: (params: { sessionId; messages; tokenBudget? }) => Promise<AssembleResult>;
  compact: (params: { sessionId; sessionFile; tokenBudget?; force?; ... }) => Promise<CompactResult>;
  afterTurn?: (params: { sessionId; messages; prePromptMessageCount; ... }) => Promise<void>;
  prepareSubagentSpawn?: (params) => Promise<SubagentSpawnPreparation | undefined>;
  onSubagentEnded?: (params) => Promise<void>;
  dispose?: () => Promise<void>;
};

type AssembleResult = {
  messages: AgentMessage[];
  estimatedTokens: number;
  systemPromptAddition?: string;  // 注入系统提示，不进入对话历史
};
```

关键返回字段 `systemPromptAddition`：将内容注入系统提示而非 messages 数组，避免被 compaction 引擎当作对话历史来压缩。

---

## lossless-claw：完整的 ContextEngine 实现

**项目定位：** 替换 OpenClaw 内置的滑动窗口压缩，用 DAG 分层摘要实现无损上下文管理。

**插件声明：** `plugins.slots.contextEngine: "lossless-claw"`

### 核心能力

| 能力 | 实现方式 |
|------|----------|
| 消息持久化 | 每条消息存入 SQLite，按 conversation 组织 |
| 分层摘要 | DAG 结构：原始消息 → 叶节点摘要 → 凝聚摘要（condensation），层层压缩 |
| 上下文组装 | 从 DAG 中组装：摘要 + 最近 N 条原始消息，管理 token 预算 |
| 按需展开 | Agent 可通过 `lcm_expand_query` 工具展开任意摘要回溯原文 |
| 大文件处理 | 检测大文件块，单独存储并生成探索摘要 |
| 子 Agent 支持 | ExpansionGrant 机制，允许子 agent 有限度访问父 agent 的 DAG |

### ContextEngine 方法实现深度

| 方法 | lossless-claw 实现 |
|------|-------------------|
| `ingest` | 持久化到 SQLite，检测大文件，生成探索摘要 |
| `assemble` | 从 DAG 组装摘要 + 原始消息，管理 token 预算，注入 `systemPromptAddition` |
| `compact` | 自主 DAG 压缩（`ownsCompaction: true`）：叶节点 → condensation → 强制清扫 |
| `afterTurn` | 后处理 ingest + 自动触发压缩 |
| `bootstrap` | 从会话文件导入/对齐历史 |
| `prepareSubagentSpawn` | 创建 ExpansionGrant，传递上下文访问权 |

**代码规模：** `engine.ts` ~2800 行，完整实现所有 ContextEngine 方法。

### 注册的 Agent 工具

| 工具 | 功能 |
|------|------|
| `lcm_grep` | 跨消息和摘要的 regex/全文搜索 |
| `lcm_describe` | 查看特定摘要的详情（轻量，不启动子 agent） |
| `lcm_expand_query` | 深度回忆：启动子 agent 展开 DAG，检索并返回带引用的答案 |

### 数据模型（SQLite）

```
conversations  → 1:N → messages    → N:M → summary_messages → summaries
                                                              ↕ (DAG)
                                                         summary_parents
                       context_items（组装视图：摘要 + 原始消息的有序列表）
                       large_files（大文件存储）
```

### 跨会话能力

lossless-claw 已有部分跨会话能力（均局限于单 SQLite 文件内）：
- `allConversations: true` 参数：搜索所有历史会话的消息和摘要
- ExpansionGrant：子 agent 跨会话 DAG 访问
- TUI transplant：将一个 conversation 的 DAG 移植到另一个

### 局限性（相对于企业场景）

- SQLite 单文件单写者，不支持多 Agent 并发
- 无 agent_id / tenant 隔离
- 无 embedding 语义检索（仅 regex + FTS5）
- 上下文组装以单 conversation 为单位，无法聚合多源上下文

---

## OpenViking：从 memory 插件演进为 context-engine 适配器

**项目定位：** 基于向量检索的长期记忆后端，自动提取和召回跨会话记忆。

### 演进历程

| 版本 | 插件目录 | 槽位 | 插件名 |
|------|----------|------|--------|
| 旧版 (memory plugin) | `examples/openclaw-memory-plugin/` | `plugins.slots.memory` | `memory-openviking` |
| 新版 (context-engine) | `examples/openclaw-plugin/` | `plugins.slots.contextEngine` | `openviking` |

新版 manifest 声明：
```json
{ "id": "openviking", "kind": "context-engine" }
```

### 核心能力

| 能力 | 实现方式 |
|------|----------|
| 记忆提取 | 对话后自动提取关键信息到 OpenViking（embedding + 向量存储） |
| 记忆召回 | 对话前自动召回相关记忆注入上下文 |
| 多模态 embedding | 支持视觉模型 embedding（doubao-embedding-vision） |
| 远程/团队共享 | Remote 模式支持多 OpenClaw 实例共享记忆 |

### ContextEngine 方法实现深度

| 方法 | OpenViking 实现 | 说明 |
|------|----------------|------|
| `ingest` | 空操作（`return { ingested: false }`） | 不做消息持久化 |
| `assemble` | 原样透传 messages | 不做上下文组装 |
| `compact` | 委托给 OpenClaw LegacyContextEngine | 不管压缩 |
| `afterTurn` | **唯一活跃方法**：auto-capture 提取记忆 | 核心价值 |

**代码规模：** `context-engine.ts` ~275 行，本质是适配器。

auto-recall（记忆召回）不是在 `assemble` 中实现的，而是在 `before_prompt_build` hook 中通过 `prependContext` 注入。

### 注册的 Agent 工具

| 工具 | 功能 |
|------|------|
| `memory_recall` | 从 OpenViking 向量库搜索相关记忆 |
| `memory_store` | 将文本存入 OpenViking 记忆管线 |
| `memory_forget` | 删除指定记忆（按 URI 或搜索匹配） |

---

## 对比总结

| 维度 | lossless-claw | OpenViking (新版) |
|------|--------------|------------------|
| **核心定位** | 会话内上下文窗口无损管理 | 跨会话长期记忆提取与召回 |
| **ContextEngine 实现** | 完整实现（替代 OpenClaw 内置引擎） | 适配器模式（仅用 afterTurn，其余透传/委托） |
| **是否 ownsCompaction** | 是（自主管理压缩） | 否（委托给 Legacy 引擎） |
| **存储** | SQLite（本地） | OpenViking 服务（向量 DB + embedding） |
| **检索方式** | regex + FTS5 | embedding 语义向量检索 |
| **跨会话** | 有限（搜索可跨，组装不跨） | 完整（记忆跨会话持久化 + 召回） |
| **多 Agent** | 仅子 agent（ExpansionGrant） | 支持（Remote 模式 + agentId 切换） |
| **上下文注入通道** | `assemble` 返回的 messages + systemPromptAddition | `before_prompt_build` hook 的 prependContext |
| **代码量** | ~2800 行（engine.ts） | ~275 行（context-engine.ts） |

**两者互补而非竞争：** lossless-claw 管"当前对话别忘了前面说的"，OpenViking 管"上次（甚至上个月）的对话里的重要信息别丢"。理论上可同时使用，但 OpenClaw 的 contextEngine slot 是单选的，新版 OpenViking 通过在 compact 中 `tryLegacyCompact()` 来部分弥补。

---

## 对 ContextHub 的设计参考价值

### 接口设计参考 lossless-claw
- `ContextEngine` 接口的完整 surface area
- `AssembleResult.systemPromptAddition` 作为上下文注入通道
- `ownsCompaction` 标志的含义和影响

### 接入模式参考 OpenViking 新版
- 适配器模式：占据 contextEngine slot 但不接管 compaction
- `compact` 中的 `tryLegacyCompact()` 委托模式
- `afterTurn` 作为记忆提取的生命周期钩子
- 同时使用 hooks（`before_prompt_build`）和 contextEngine 方法

### ContextHub 采用的架构决策
详见 08-architecture.md "OpenClaw 插件架构决策"小节。

---

## db9：面向 Agent 的统一存储平台

**项目地址：** [db9.ai](https://db9.ai/)
**前身项目：** [agfs](https://github.com/c4pt0r/agfs) — Agent 文件系统，后被火山引擎 OpenViking 采用为存储组件

### 核心定位

db9 的目标是成为 Agent 的通用存储后端（"Postgres but for agents"），将 SQL（PostgreSQL 协议）和文件系统两个心智模型统一在一个平台中。

作者的核心洞察：当今 coding agent 依赖两个主要接口 — SQL 和文件系统（`ls` / `cp` / `grep` 等 Unix 工具），但这两者在传统架构中是分裂的。db9 通过 `fs9` 文件系统扩展将二者融合：文件可通过 SQL 查询（`extensions.fs9('/path')`），SQL 结果也可写回文件系统。

### 关键能力

| 能力 | 实现方式 | 备注 |
|------|----------|------|
| SQL + 文件系统融合 | `fs9` 扩展：文件可在 SQL 中作为虚拟表查询 | 支持 CSV/JSONL/Parquet |
| 内置 embedding | `embedding()` SQL 函数，无需外部 pipeline | 替代独立向量 DB + embedding 服务 |
| 向量搜索 | `vec <-> embedding('query')` 语法 | 原生 SQL 语义 |
| 环境分支 | `db9 branch create` 克隆整个环境（数据+文件+cron+权限） | 对测试/staging 有价值 |
| 文件存储 | `db9 fs cp` / `db9 fs mount` | 替代 S3 |
| Cron 调度 | SQL 或 CLI 创建定时任务 | 分布式调度，无 idle timeout |
| HTTP 调用 | `http_get()` / `http_post()` SQL 函数 | 数据库内直接调用外部 API |
| 多租户 | 每个 agent/用户一个独立数据库实例 | Serverless，基于 TiDB X 引擎 |
| Skill onboarding | `skill.md` 自描述，Agent 自主学习使用 | 零配置接入 |

### 典型使用范式

db9 将 Agent 数据管理分为三类场景：

| 场景 | 结构化数据（Postgres 表） | 非结构化数据（文件系统） |
|------|--------------------------|------------------------|
| Memory（记忆） | 会话元数据、偏好索引 | 会话 transcript、snapshot |
| Knowledge（知识） | 文档 chunks、向量、元数据 | 源文档原文 |
| Outputs（输出） | 运行历史、状态追踪 | 报告、日志、截图 |

### 与 agfs 的关系

agfs 是同一作者早期的开源 Agent 文件系统尝试，后被火山引擎用于 OpenViking 作为存储组件。agfs 的设计理念（Agent 通过文件系统语义操作数据）被融合进 db9 的 `fs9` 文件系统中。这条线索将 agfs → OpenViking（文件存储组件）→ db9（SQL+FS 统一平台）串联起来。

---

## db9 与 ContextHub 的对比分析

### 架构层级差异

db9 和 ContextHub 处于架构栈的不同层级：

```
用户 → OpenClaw (Agent Runtime) → ContextHub (上下文管理中间件) → PostgreSQL (存储层)
                                                                    ↑
                                                             db9 处于此处
```

- **db9 是存储层**：提供数据库 + 文件系统的统一接口，不包含领域特定的上下文管理逻辑
- **ContextHub 是中间件**：核心价值在 L0/L1/L2 分层模型、变更传播、多 Agent 协作、ACL 语义 — 这些是应用层逻辑

### 哲学对偶

| 维度 | db9 | ContextHub |
|------|-----|-----------|
| 出发点 | 从存储出发：设计让 Agent 好用的存储接口 | 从 Agent 需求出发：设计让数据好管的上下文系统 |
| 接口哲学 | Agent 同时看到 SQL 和 FS 两个接口，可互操作 | Agent 只看到 `ctx://` 文件语义，PG 对 Agent 透明 |
| 文件的角色 | 文件是入口和中心（数据进入后系统自动理解） | 文件语义是外壳（URI 不对应物理文件，而是 PG 行） |
| 多租户模型 | per-instance 隔离（每个 agent 一个独立 DB） | shared-DB + RLS（所有 agent 共享一个 PG，行级隔离） |
| 目标用户 | C 端 agent/开发者（规模：百亿级轻量租户） | B 端企业（规模：数十到数百 agent，数据复杂度高） |

### 为什么 ContextHub 不基于 db9 构建

评估过将 db9 作为 ContextHub 存储层替代标准 PostgreSQL 的方案，最终决定不采用。关键原因：

1. **多租户模型根本性冲突。** db9 的 per-instance 模型下，ContextHub 的跨 Agent 操作（`dependencies` JOIN、记忆晋升、变更传播）变成跨数据库操作，需要应用层实现分布式事务。ContextHub 的 shared-DB + RLS 模型让这些操作都是单库 SQL。

2. **PG 高级特性可用性。** ContextHub 重度依赖 `LISTEN/NOTIFY`（传播）、自定义 RLS 策略、`SET LOCAL` GUC（租户上下文）、递归 CTE（血缘）、触发器。db9 作为 serverless managed service，对这些高级特性的支持程度未知。

3. **部署约束。** ContextHub 面向 toB 企业，需要 on-premise 私有化部署。db9 目前仅提供 SaaS 模式。

4. **延迟。** 上下文检索是 hot path，远程 managed service 增加一轮网络往返（50-200ms），自管 PG 同机部署延迟 1-5ms。

5. **Vendor 依赖风险。** 将企业级系统的存储层绑定在一个新兴平台上，风险与收益不对等。

### 从 db9 借鉴的理念

虽然不基于 db9 构建，但以下理念对 ContextHub 有参考价值：

**1. 向量索引内置于数据库（已采纳）**

db9 的 `embedding()` SQL 函数验证了"embedding 内置于数据库"的可行性。ContextHub 已将独立向量库（ChromaDB/Milvus）替换为 pgvector 扩展，L0 embedding 与元数据同库同事务，消除双写对账问题。

**2. "文件作为入口"的数据导入范式（可探索）**

db9 作者强调"文件作为中心（以及入口）。数据只要进入，系统便可以理解一切"。ContextHub 的 CatalogConnector 当前是 API 驱动的拉取模式，可考虑增加文件推送模式 — 用户/DBA 直接上传 DDL 文件或 CSV 样本数据，系统自动解析并生成 L0/L1 上下文，降低非 Agent 用户的接入门槛。

**3. pg_cron 下沉定时任务（可选采纳）**

db9 将 cron 内置于数据库层。ContextHub 的 Lifecycle Manager 和 CatalogSync 可考虑用 pg_cron 扩展执行简单的定时 SQL（如归档过期上下文），减少应用层定时任务。

**4. 环境分支用于评估实验**

db9 的 `branch create` 启发了 ContextHub 的 A/B 消融实验可以利用 PG 环境快照（`pg_dump` + `pg_restore` 或 Docker volume snapshot）实现更干净的实验隔离。
