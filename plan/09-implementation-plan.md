# 09 — 实施计划、验证方案与技术选型

## MVP 验证场景

单 OpenClaw 实例 + agent_id 切换，验证多 Agent 协作的企业数据分析：

1. 数据查询 Agent（agent_id="query-agent"）：自然语言 → ContextHub 检索湖表元数据 + 查询模板 → 生成 SQL → 执行
2. 数据分析 Agent（agent_id="analysis-agent"）：查询结果 → ContextHub 检索业务知识 + 分析 patterns → 生成报告
3. 协作验证：query-agent 积累的成功 SQL pattern 提升为共享记忆 → 切换到 analysis-agent 身份验证可见性
4. 传播验证：以 query-agent 身份更新 Skill → Server 端自动传播 → 切换到 analysis-agent 身份验证依赖被标记 stale

### 为什么单实例够用

ContextHub 的协作机制（传播、ACL、记忆晋升、反馈生命周期）全部是 Server 端状态机，不依赖多个活跃 Agent 运行时：
- 传播引擎：Agent A 更新 Skill → PG NOTIFY → Server 查依赖图 → 标记 Agent B 的依赖为 stale。Agent B 不需要在线
- 记忆晋升：一次 API 调用，Server 端写入 + 建依赖关系
- ACL 验证：Server 端在查询时做 RLS 过滤
- 反馈生命周期：Server 端根据 adopted/ignored 计数更新质量分

因此，单 OpenClaw 实例通过 SDK 调用时切换 `agent_id` 参数即可验证所有协作特性。多实例场景（多人各自用 OpenClaw 协作分析同一数据集）属于产品成熟期目标。

### DataAgent 对接方式

采用 OpenClaw context-engine 插件模式（参考 OpenViking 新版 openclaw-plugin 的 context-engine 架构，详见 13-related-works.md）。Plugin 声明 `kind: "context-engine"`，注册为 `plugins.slots.contextEngine`。

**注册的 Agent 工具：**

| Tool | 功能 | 对应 SDK 方法 |
|------|------|--------------|
| `ls` | 列出 ctx:// 路径下的子项 | `ctx.ls()` |
| `read` | 读取上下文内容（L0/L1/L2） | `ctx.read()` |
| `grep` | 语义搜索上下文 | `ctx.search()` |
| `stat` | 查看上下文元信息 | `ctx.stat()` |
| `contexthub_store` | 写入记忆/案例 | `ctx.memory.add_case()` |
| `contexthub_promote` | 提升记忆到团队共享 | `ctx.memory.promote()` |
| `contexthub_skill_publish` | 发布 Skill 新版本 | `ctx.skill.publish()` |
| `contexthub_feedback` | 报告上下文采纳/忽略 | `ctx.feedback.report()` |

**ContextEngine 生命周期方法：**

| 方法 | 行为 |
|------|------|
| `assemble` | 透传 messages，通过 `systemPromptAddition` 注入 PG auto-recall 结果 |
| `afterTurn` | 自动提取记忆写入 PG（auto-capture） |
| `compact` | 委托给 OpenClaw LegacyContextEngine |
| `ingest` / `ingestBatch` | 空操作 |

这种"增强型适配器"模式确保 ContextHub 的上下文注入不会被 compaction 引擎当作对话历史来压缩（详见 08-architecture.md "OpenClaw 插件架构决策"）。

## SDK 对接方式

```python
from contexthub import ContextHubClient

ctx = ContextHubClient(url="http://localhost:8000", api_key="...")

# 检索湖表元数据
tables = await ctx.search("月度销售额统计", scope="datalake", level="L1")

# 检索历史查询 cases
cases = await ctx.search("销售额统计 SQL", scope="agent_memory", category="cases")

# 记录成功的查询为 case
await ctx.memory.add_case(
    content="SELECT ... GROUP BY month",
    context={"question": "月度销售额", "tables_used": ["orders", "products"]}
)

# 提升为团队共享
await ctx.memory.promote(
    uri="ctx://agent/my-agent/cases/xxx",
    target_team="engineering/backend"
)
```

## 自定义 Benchmark: ECMB

### 设计原则

评估分为三个层级，按量化可信度从高到低排列：
- Tier 1（量化 Benchmark）：有客观 ground truth、可画曲线的硬指标
- Tier 2（A/B 消融实验）：隔离单个特性的贡献，对比有/无该特性的效果差异
- Tier 3（功能正确性测试）：pass/fail 的集成测试，验证架构能力而非优化指标

### 测试数据集

**数据湖模拟（核心数据集）**

基于 BIRD 数据集改造，增加企业级元素：

| 数据项 | 数量 | 来源 |
|--------|------|------|
| 数据库 | 5-8 个 | BIRD 子集（选业务复杂度高的库：financial, retail, healthcare 等） |
| 表 | 30-50 张 | BIRD 原始表 + 补充 JOIN 关系和血缘 |
| 自然语言问题 | 200-300 条 | BIRD 原始问题 + 自构造多表 JOIN / 业务术语问题 |
| Gold SQL | 与问题一一对应 | BIRD 原始 + 人工标注 |
| 表间 JOIN 关系 | 40-60 条 | 从 BIRD 的 FK 关系提取 + 补充 common_join |
| 血缘关系 | 15-20 条 | 自构造（ODS → DWD → DWS 三层） |
| 查询模板 | 每表 3-5 条，共 100-150 条 | 从 Gold SQL 中提取通用模式 |
| 业务术语 | 30-50 条 | 自构造（如"GMV"="SUM(amount) WHERE status='completed'"） |

**变更传播场景数据**

| 场景 | 数量 | 说明 |
|------|------|------|
| Schema 变更事件 | 10-15 次 | ALTER TABLE（加字段、改类型、删字段） |
| 依赖关系 | 每张表 2-4 条依赖，共 60-100 条 | skill_version + table_schema + derived_from |
| Skill 版本更新 | 5-8 次 | 其中 3-4 次 is_breaking=True |
| 记忆晋升事件 | 5-8 次 | agent private → team shared |

**多 Agent 工作流数据**

| 场景 | 数量 | 说明 |
|------|------|------|
| query-agent 学习 episode | 10-15 轮 | 每轮：问题 → 检索 → 生成 SQL → 执行 → 记录 case |
| analysis-agent 分析任务 | 8-10 轮 | 依赖 query-agent 的共享记忆和查询结果 |
| 跨 Agent 协作场景 | 3-5 个 | 端到端：查询 → 分析 → 报告 |

### Tier 1: 量化 Benchmark

这些指标有客观 ground truth，可以在不同配置间做统计显著性比较。

| 指标 | 量化方式 | Ground Truth | 预期信号 |
|------|----------|-------------|----------|
| SQL Execution Accuracy (EX) | 生成 SQL 在数据库上执行结果与 Gold SQL 结果一致的比例 | BIRD Gold SQL | ContextHub 结构化上下文 > Baseline 平坦 RAG |
| Table Retrieval Precision@5 | 检索到的 top-5 表中，Gold SQL 实际用到的表的比例 | Gold SQL 中的 FROM/JOIN 表 | L0/L1 两阶段 > 单阶段向量检索 |
| Table Retrieval Recall | Gold SQL 用到的表被检索到的比例 | 同上 | 结构化关系辅助 > 纯语义匹配 |
| Token per Query (检索阶段) | 每次查询注入 LLM 的上下文 token 数 | 直接计数 | L0/L1/L2 分层 << 全量 schema dump |
| Token per Query (端到端) | 包含 LLM 生成的总 token 消耗 | 直接计数 | 更精准的上下文 → 更短的推理链 |
| Propagation Token Cost | 一次变更事件触发的传播总 token 消耗 | 直接计数 | 三级规则 << 全 LLM 重评估 |
| E2E Latency P50/P99 | 从用户提问到返回 SQL 的端到端延迟 | 直接计时 | PG JOIN 组装 vs 多次向量库查询 |

### Tier 2: A/B 消融实验

每个实验隔离一个特性，其他条件保持一致。使用 Tier 1 的指标做对比。

**实验 1：L0/L1/L2 分层检索 vs 平坦 RAG**

| 配置 | 说明 |
|------|------|
| A (Baseline) | 所有表 schema 切成 ~500 token 的 chunk，存入向量库，检索 top-K chunk 直接塞入 prompt |
| B (ContextHub) | L0 向量检索 → L1 精排 → 选择性加载 L2 结构化数据 |
| 对比指标 | EX, Token per Query, Table Precision@5 |
| 预期结果 | B 的 Token 显著降低（60-80%），EX 持平或略高（因为噪音更少） |

**实验 2：有/无结构化关系（table_relationships + query_templates）**

| 配置 | 说明 |
|------|------|
| A | ContextHub 检索但不注入 JOIN 关系和查询模板 |
| B | ContextHub 检索 + PG JOIN 组装完整上下文（关系 + 模板 + 业务术语） |
| 对比指标 | EX（尤其是多表 JOIN 子集）, Token per Query |
| 预期结果 | B 在多表 JOIN 查询上 EX 显著提升（这是结构化元数据的核心价值） |
| 子集分析 | 按查询复杂度分层：单表 / 2 表 JOIN / 3+ 表 JOIN / 含业务术语 |

**实验 3：有/无变更传播（schema 变更后的 SQL 质量）**

| 配置 | 说明 |
|------|------|
| 时序设计 | T0: 正常状态，跑一轮 SQL 生成 → T1: 模拟 schema 变更（加字段/改类型） → T2: 再跑一轮 SQL 生成 |
| A | 无传播：T2 时 Agent 仍使用 T0 的旧 context（stale schema、旧查询模板） |
| B | 有传播：T1 变更触发传播引擎 → 标记 stale → 重新生成 L0/L1 → T2 时 Agent 用更新后的 context |
| 对比指标 | T2 的 EX 差异, 传播 token 消耗 |
| 预期结果 | A 在 T2 的 EX 下降（用了过时 schema），B 维持 T0 水平 |

**实验 4：有/无共享记忆（冷启动 vs 知识继承）**

| 配置 | 说明 |
|------|------|
| A (冷启动) | analysis-agent 没有任何历史记忆，从零开始 |
| B (知识继承) | query-agent 先跑 10-15 轮积累 cases → 晋升为团队共享 → analysis-agent 可检索 |
| 对比指标 | analysis-agent 的 EX, 首次正确回答所需轮次 |
| 预期结果 | B 的 EX 更高，尤其在 query-agent 已解决过的类似问题上 |
| 诚实说明 | 这个实验本质上测的是"更多相关上下文是否提升准确率"，信号可能与实验 2 重叠 |

### Tier 3: 功能正确性测试（pass/fail）

这些不是 benchmark 指标，而是集成测试。用 pytest + httpx 直接测 ContextHub Server API。

**变更传播正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| P-1: Schema 变更 → 依赖标记 stale | ALTER TABLE orders ADD COLUMN discount DECIMAL | 所有依赖 orders 的 cases/patterns status='stale' |
| P-2: Breaking Skill 更新 → 订阅者标记 stale | 发布 sql-generator v3 (is_breaking=True) | 所有 pinned_version < 3 的订阅者的依赖 context status='stale' |
| P-3: Non-breaking Skill 更新 → 仅通知 | 发布 sql-generator v4 (is_breaking=False) | 依赖方 status 不变，change_events 有通知记录 |
| P-4: 统计信息更新 → 不传播 | UPDATE table_metadata SET stats = ... | 无 change_event，无 NOTIFY |
| P-5: 级联传播 | 表 A schema 变 → Skill X 依赖 A 被标记 stale → Case Y 依赖 Skill X | Case Y 也被标记 stale（二级传播） |
| P-6: 表删除 → 归档 + 传播 | CatalogConnector 检测到表被删除 | 表 context status='archived'，依赖方 status='stale' |

**多 Agent 协作正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| C-1: 记忆晋升 | query-agent 的 case → promote 到 team/engineering | team 路径下可查到，dependencies 有 derived_from 记录 |
| C-2: 晋升后可见性 | analysis-agent search scope=team | 能检索到 query-agent 晋升的记忆 |
| C-3: 源记忆变更传播 | 修改 query-agent 的原始 case | 晋升后的团队记忆收到通知（derived_from 依赖） |
| C-4: Skill 版本订阅 | analysis-agent 订阅 sql-generator, pinned_version=2 | 检索时返回 v2 内容，不受 v3 发布影响 |
| C-5: Skill 订阅 latest | analysis-agent 订阅 sql-generator, pinned_version=NULL | 检索时返回最新版本 |

**ACL 隔离正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| A-1: Agent 私有隔离 | query-agent 写入 ctx://agent/query-agent/... → analysis-agent 尝试读取 | 403 或空结果 |
| A-2: 团队层级继承 | 写入 ctx://team/engineering/ → backend 子团队 agent 读取 | 可见 |
| A-3: Deny override | 父团队 deny 某资源 → 子团队 allow 同一资源 | deny 生效（deny-override） |
| A-4: 字段脱敏 | 标记某字段为 sensitive → Agent 检索 | 字段值替换为 [MASKED] |

**反馈与生命周期正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| F-1: 隐式反馈记录 | 检索 context → Agent 使用（adopted） | context_feedback 表有记录，adopted_count +1 |
| F-2: 质量评分计算 | 10 次 adopted + 5 次 ignored | quality_score = 10/(10+5+1) ≈ 0.625 |
| F-3: 低质量报告 | 构造高检索低采纳的 context | 出现在低质量报告 SQL 结果中 |
| F-4: 生命周期状态机 | active → 超过 stale_after_days 未访问 | status 变为 stale |
| F-5: Stale 自动恢复 | stale context 被直接访问 | status 恢复为 active |

### 量化可行性总结

| ContextHub 设计优势 | 可量化程度 | 最佳指标 | 评估层级 |
|---------------------|-----------|----------|----------|
| L0/L1/L2 分层检索 | 强 | Token per Query（预期降低 60-80%） | Tier 1 + A/B 实验 1 |
| 结构化上下文组装（关系+模板+术语） | 强 | 多表 JOIN 的 EX（预期提升显著） | Tier 1 + A/B 实验 2 |
| 精确变更传播（stats≠schema） | 强（token）| Propagation Token Cost（预期节省 99%） | Tier 1 + A/B 实验 3 |
| 传播后下游质量保持 | 中 | Schema 变更后 EX 不下降 | A/B 实验 3 |
| 多 Agent 知识共享 | 弱（与检索重叠）| 冷启动 vs 知识继承的 EX 差异 | A/B 实验 4 |
| ACL/隔离/版本管理 | 不可量化 | pass/fail | Tier 3 功能测试 |
| 反馈生命周期 | 不可量化（需长期数据）| pass/fail | Tier 3 功能测试 |

## 实施计划（并行推进）

### Phase 1: 项目骨架 & 核心抽象（1-2 周）
1. 初始化项目（Python, FastAPI, pyproject.toml）
2. PG 数据库初始化：
   - 创建所有核心表（contexts, dependencies, change_events, table_metadata, lineage, table_relationships, query_templates, skill_versions, access_policies, audit_log, team_memberships, lifecycle_policies, context_feedback）
   - 配置 RLS 策略
   - 创建索引
3. 实现 ContextStore（URI 路由层）：ctx:// URI → PG 读写 + ACL 检查
4. 实现 pgvector 集成：L0 embedding 列 + HNSW 索引，写入/检索与 PG 事务统一
5. 实现 MockCatalogConnector（硬编码几张表的元数据）
6. 设计并实现 Python SDK 接口

### Phase 2: 两条线并行（3-4 周）

线 A — 数据湖表上下文管理：
7. 湖表元数据的 L0/L1 自动生成（CatalogConnector → LLM 生成摘要 → 写入 PG）
8. Retrieval Engine（向量检索 L0 → PG 读 L1 精排 → 按需加载结构化数据）
9. Text-to-SQL 上下文组装逻辑（PG JOIN 查询：schema + 关系 + 模板 + 业务术语）
10. 实现 ContextHub OpenClaw Plugin，对接 DataAgent，跑通"自然语言 → 上下文检索 → SQL 生成"链路

线 B — 多 Agent 协作 + 变更传播 + 质量闭环：
11. 多层级团队模型（team_memberships 表）+ ACL（access_policies 表 + RLS + deny-override）
12. Memory Service（提取、去重、热度更新、共享提升 — 全部 PG 事务操作）
13. Skill Service（skill_versions 表、发布/订阅、is_breaking 标记）
14. Propagation Engine（PG LISTEN/NOTIFY + dependencies 表查询 + PropagationRule 三级响应）
15. Feedback Collector（context_feedback 表 + adopted/ignored 计数更新 + 低质量报告 SQL）
16. Lifecycle Manager（contexts.status 状态机 + lifecycle_policies 表 + 定时 SQL 任务）

### Phase 3: 集成与评估（2-3 周）

**Step 17: 测试数据集准备（3-4 天）**
- 从 BIRD 数据集选取 5-8 个业务复杂度高的数据库，提取 30-50 张表
- 补充企业级元素：table_relationships（40-60 条 JOIN 关系）、lineage（15-20 条血缘）、query_templates（100-150 条）、business_terms（30-50 条）
- 筛选 200-300 条自然语言问题，按复杂度标注：单表 / 2 表 JOIN / 3+ 表 JOIN / 含业务术语
- 构造变更传播场景：10-15 次 schema 变更事件、5-8 次 Skill 版本更新
- 构造多 Agent 工作流：query-agent 10-15 轮学习 episode、5-8 次记忆晋升

**Step 18: Tier 1 量化 Benchmark + Tier 2 A/B 消融实验（5-7 天）**
- 实现 Baseline：平坦 RAG（所有 schema 切 chunk → 向量检索 → top-K 塞入 prompt）
- 跑 4 组 A/B 实验，每组对比 Tier 1 指标（EX, Token, Precision@5, Latency）
- 按查询复杂度子集分析结果（单表 / 多表 JOIN / 业务术语）
- 统计显著性检验（paired t-test 或 bootstrap）

**Step 19: Tier 3 功能正确性测试（2-3 天）**
- pytest + httpx 实现所有 Tier 3 测试用例（传播 P-1~P-6、协作 C-1~C-5、ACL A-1~A-4、反馈 F-1~F-5）
- CI 集成，确保每次代码变更不破坏功能正确性

**Step 20: 评估报告（1-2 天）**
- 汇总 Tier 1 指标对比表 + Tier 2 消融实验结果
- 按设计优势维度组织：分层检索效果、结构化组装效果、传播效果、协作效果
- 诚实标注各指标的置信度和局限性
- 输出可复现的评估脚本和数据集

## 技术选型

| 组件 | 推荐 | 理由 |
|------|------|------|
| Web 框架 | FastAPI | 异步、类型安全、OpenAPI 自动生成 |
| 主数据库 | PostgreSQL | 元数据 + 内容统一存储，ACID 事务，LISTEN/NOTIFY 驱动传播，RLS 租户隔离，递归 CTE 血缘查询 |
| PG 驱动 | asyncpg | 高性能异步 PG 客户端，原生支持 LISTEN/NOTIFY |
| 向量索引 | pgvector（PG 扩展） | 向量与元数据同库，天然事务一致，无双写对账问题。L0 摘要量级（万级）HNSW 索引完全胜任。消除独立向量库依赖，MVP 架构最简 |
| PG 扩展（可选） | pg_cron | 下沉定时任务（生命周期归档、CatalogSync）到数据库层 |
| Embedding | text-embedding-3-small 或 BGE-M3 | 成本/效果平衡 |
| LLM（摘要生成） | Claude / GPT-4o-mini | L0/L1 生成不需要最强模型 |
| DB Migration | Alembic | PG schema 版本管理 |
| 测试 | pytest + pytest-asyncio | Python 标准 |

### 不再需要的组件

| 原方案组件 | 状态 | 原因 |
|-----------|------|------|
| ContentStore 接口（S3/LocalFS） | 移除 | 内容存 PG TEXT 列 |
| Event Log（JSON / Redis Streams） | 移除 | 变更事件存 PG change_events 表 |
| 独立审计日志存储 | 移除 | 审计日志存 PG audit_log 表 |
| 独立向量数据库（ChromaDB / Milvus） | 移除 | 向量索引由 pgvector 扩展承担，与 PG 同库，消除双写对账和额外基础设施 |

## 参考资料

- [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源
- [Mem0](https://mem0.ai/blog/multi-agent-memory-systems) — 多 Agent 记忆系统
- [Letta Memory Blocks](https://www.letta.com/blog/memory-blocks) — Memory Block 设计
- [ContextBench](https://www.sundeepteki.org/blog/context-bench-a-benchmark-for-evaluating-agentic-context-engineering) — Agentic Context Engineering 评估
- [Spider](https://yale-lily.github.io/spider) / [BIRD](https://bird-bench.github.io/) — Text-to-SQL 评估数据集
