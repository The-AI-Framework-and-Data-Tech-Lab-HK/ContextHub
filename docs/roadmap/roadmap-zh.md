# ContextHub 路线图

本文档基于 `task-prompt/` 下各 Phase 的实施计划，汇总各阶段的目标、核心产出和当前状态。

> 各 Phase 之间是严格递进关系——后续 Phase 在前序 Phase 的冻结基线上叠加新能力，且不破坏已冻结的语义。

---

## Phase 1 — MVP 核心 ✅

**一句话**：从零构建 ContextHub 的完整 MVP 闭环——数据库 schema、`ctx://` URI 路由、四大核心服务、Python SDK、OpenClaw 插件和首个垂直载体（数据湖表管理），跑通"私有写入 → 晋升共享 → 跨 Agent 复用 → Skill 更新 → 下游 stale/advisory 感知 → 补偿恢复"的横向协作链路。

### 核心产出


| 模块                            | 目标                                                                                                                         |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **基础设施**                      | 项目骨架（FastAPI + asyncpg + Alembic）、PostgreSQL schema（含 RLS + pgvector）、Pydantic 模型、`ScopedRepo` request-scoped DB 执行模型      |
| **ContextStore + ACLService** | `ctx://` URI 路由与读写层、默认可见性（子读父、父不读子）、默认写权限（ownership + team_memberships）、认证中间件                                              |
| **MemoryService**             | 私有记忆写入、记忆晋升（`私有 → 团队 → 组织`）、`derived_from` 血缘追踪                                                                            |
| **SkillService**              | Skill 版本发布（不可变语义）、`is_breaking` 标记、订阅管理、版本解析（latest / floating / pinned / `?version=N`）                                    |
| **RetrievalService**          | pgvector 向量检索 → BM25 精排 → 可见性过滤、L0/L1/L2 按需加载、embedding 降级策略、Tools API（`ls`/`read`/`grep`/`stat`）                          |
| **PropagationEngine**         | Outbox 事件消费（claim → process → finish）、依赖传播（skill_version / table_schema / derived_from）、订阅通知、幂等副作用、LISTEN/NOTIFY 唤醒 + 周期补扫 |
| **Python SDK**                | Typed async client，封装所有 MVP API                                                                                            |
| **OpenClaw Plugin**           | 7 个 Agent 工具（`ls`/`read`/`grep`/`stat`/`store`/`promote`/`skill_publish`）、`assemble` 自动上下文注入、`afterTurn` 自动记忆提取            |
| **数据湖载体**                     | Catalog 同步（MockCatalogConnector）、表元数据 L0/L1 自动生成、`sql-context` 上下文组装、Embedding 对账（ReconcilerService）                       |
| **集成测试**                      | Tier 3 功能正确性测试（P-1P-8、C-1C-5、A-1A-4）、端到端 demo 脚本                                                                           |


### 冻结的关键设计决策

- **单数据库架构**：PostgreSQL 承载元数据 + 内容 + 向量 + 事件，无外部向量库、无消息队列
- **Request-scoped DB 执行模型**：所有 SQL 通过 `ScopedRepo` 执行，`SET LOCAL app.account_id` 绑定租户
- **RLS 租户隔离**：所有面向 Agent 的表启用行级安全
- **文件系统范式**：Agent 使用 `ls`/`read`/`grep`/`stat` 操作上下文，LLM 天然理解

---

## Phase 2 — 显式 ACL 与审计 ✅

**一句话**：在 Phase 1 的默认可见性 / 默认写权限基线之上，叠加**读路径**的显式 ACL allow/deny/field mask、分层审计日志，以及基于 ACL share grant 的跨团队窄范围共享，使 ContextHub 具备企业级的细粒度读访问控制与合规审计能力。

### 具体目标

1. **显式 ACL allow/deny**：管理员可为任意 `ctx://` URI 模式设置读路径的 allow/deny 策略，deny 永远优先于 allow（deny-override）
* **通俗含义**：管理员可以设置"谁能读什么"的规则，而且"禁止"永远比"允许"优先。Phase 1 里，数据的可见范围是固定规则（子团队能看父团队的东西，反过来不行）。Phase 2 要在这个基础上加一层"手动开关"——管理员可以指定某些路径的数据，谁可以读、谁不可以读。
* **举例**：
  - 管理员设了一条规则："销售团队**不能**读 `ctx://datalake/prod/salary/*`（薪资数据）"
  - 又设了一条："HR 团队**可以**读 `ctx://datalake/prod/salary/*`"
  - 如果某个人同时属于销售团队和 HR 团队，结果是——**不能读**。因为"禁止"永远赢过"允许"。

2. **团队层级约束**：父团队的 deny 不可被子团队的 allow 覆盖——从根团队向下遍历，任一层级的 deny 即终止
* **通俗含义**：组织架构是层级的（比如公司 → 部门 → 小组）。上级团队的禁令，下级团队无法推翻。即如果公司级别说"这个数据不准看"，部门级别或小组级别不能自行解禁。
* **举例**：
  - 公司根团队设了禁令："任何人不准读薪资数据"
  - HR 部门管理员想给自己团队加一条"允许读薪资数据"
  - 结果：**无效**。上级的"禁止"是铁律，下级无法用"允许"绕过。

3. **关键词级脱敏（field mask）**：检索/读取结果中敏感关键词替换为 `[MASKED]`（正则级文本替换，返回时过滤，不在存储层加密）
* **通俗含义**：即使你有权读某条数据，管理员也可以指定某些敏感词（比如"salary""ssn"）在返回结果中被替换成 `[MASKED]`，AI Agent 看到的就是脱敏后的版本。
* **举例**：
  - 一条上下文原文是："该员工的 salary 为 50000 元，ssn 为 320xxx"
  - 管理员对这个路径设了 field_masks: `["salary", "ssn"]`
  - AI Agent 检索到这条上下文时，看到的是："该员工的 [MASKED] 为 50000 元，[MASKED] 为 320xxx"

4. **分层审计日志**：状态变更操作（即create/update/delete/promote/publish/policy_change等写操作）fail-closed 保证不丢失；观察性操作（即read/search/ls/stat等读操作）best-effort 保证不拖死主流程
* **通俗含义1 (关于写操作)**：写审计日志和做这个操作是**绑定在一起的**。如果审计日志写失败了，**这个操作本身也不允许成功**。宁可操作失败，也不能让操作悄悄发生却没有记录。
* **举例**：
  - 小张调用 API 删除了一条敏感上下文
  - 系统在执行删除的同时，必须在审计日志里记下"小张在 2026-04-10 14:30 删除了 ctx://prod/secrets/db-password"
  - **如果这条审计日志因为数据库故障写不进去，那删除操作本身也会被回滚** —— 删除不会生效
  - 这就是"fail-closed"：审计记录写不进去 → 关闭操作通道 → 绝不会出现"偷偷删了但没人知道"的情况
* **通俗含义2 （关于读操作）**：系统**尽量**记录这些操作，但如果审计日志写失败了（比如日志队列满了、日志服务暂时不可用），**不影响用户的正常读取操作**。用户该能搜到的数据照样搜到，只是这次读取操作可能没有被记录。
* **举例**：
  - 小李调用 API 搜索"所有关于部署流程的上下文"
  - 系统正常返回搜索结果给小李
  - 同时，系统**尝试**在审计日志里记下"小李在 2026-04-10 15:00 搜索了关键词 '部署流程'"
  - **如果这条审计日志写失败了（比如日志服务临时超时），搜索结果照样返回给小李**，不受影响
  - 这就是"best-effort"：尽力记，但不为了记日志而让用户干等着或报错
* **为什么这样设计？** （1）写操作会改变真实数据，如果没有记录，出了安全事件就无法溯源。在合规审计场景下，这是硬性要求。（2）而读操作的频率远高于写操作（可能是 100:1 甚至更高）。如果每次读操作都要求"日志写不进去就报错"，那日志系统一旦有波动，整个系统的所有读取都会瘫痪——为了记录"谁看了什么"而导致"谁都看不了"，得不偿失。

5. **跨团队 ACL share grant**：通过 ACL allow 策略授权窄范围跨团队共享（不复制内容、不创建 proxy context），作为 promote 之外的第二条共享路径
* **通俗含义**：Phase 1 已经有一种跨团队共享方式叫"promote"（把数据提升到上级团队，大家都能看到）。Phase 2 新增一种更精细的方式——通过 ACL 策略直接授权"某个团队可以读我的某条数据"，不需要把数据复制一份，也不需要把数据挪到公共层级。
* **举例**：
  - 后端团队有一条上下文 `ctx://backend/api-design`
  - 前端团队需要看这条数据来对接 API，但后端团队不想把它 promote 到公司级别（因为其他团队不需要看）
  - 后端管理员设一条 ACL 策略："允许前端团队读 `ctx://backend/api-design`"
  - 前端团队就能直接读到这条数据，**原文不动、不复制**。

6. **策略管理 API + 审计查询 API**：CRUD endpoints for `access_policies` 和审计日志检索

7. **SDK / Plugin 扩展**：客户端方法支持新 ACL/audit 能力
* 注意：Phase 2 的 ACL 策略只控制"谁能读什么"。"谁能改什么、谁能删什么"继续沿用 Phase 1 的简单规则（谁创建的谁能改，同团队的人能改）。写权限的精细化控制留到以后再做。

### 核心产出


| 产出                  | 说明                                                               |
| ------------------- | ---------------------------------------------------------------- |
| `access_policies` 表 | 显式 ACL 策略存储（uri_pattern, effect, actions, field_masks, priority） |
| `audit_log` 表       | 审计日志存储（fail-closed + best-effort 分层）                             |
| ACL 读路径评估引擎         | 两层访问模型：默认基线（Phase 1）→ 显式 ACL 覆盖（Phase 2）                         |
| MaskingService      | 检索/读取结果的关键词脱敏                                                    |
| AuditService        | `log_strict` / `log_best_effort` 分层审计                            |


### 设计约束

- **写路径不变**：Phase 2 只覆盖 `read` action，写权限继续沿用 Phase 1 的 ownership + team_memberships 模型
- **ACL 是覆盖层**：对默认基线的覆盖，不是取代基线的全局白名单系统；无匹配策略时回退到默认访问基线

---

## Phase 3 — 反馈闭环、生命周期管理与长文档检索

**一句话**：引入**质量反馈信号**驱动检索排序优化，实现**自动生命周期状态转换**治理上下文膨胀，并补全**长文档结构化检索**能力以支持 MB 级章节化文档的精确定位。

### 具体目标

#### 反馈闭环

1. **显式反馈采集**：Agent 通过 `contexthub_feedback` tool 报告上下文的采纳/忽略结果（adopted / ignored / corrected / irrelevant），反馈信号回写到 `contexts` 表
* **通俗含义**：Phase 1/2 的检索系统是"开环"的——系统把上下文推荐给 Agent，但不知道 Agent 到底用了没有、好不好用。Phase 3 要"闭环"：Agent 用完上下文后，主动告诉系统"这条我用了"或"这条没用"，让系统知道每条上下文的真实价值。
* **举例**：
  - 系统向 query-agent 推荐了 5 条上下文来帮助生成 SQL
  - query-agent 生成完 SQL 后，通过 `contexthub_feedback` tool 报告："第 1、3 条我用了（adopted），第 2、5 条没用上（ignored），第 4 条和问题完全不相关（irrelevant）"
  - 这些反馈信号被写入数据库，后续检索时系统就知道哪些上下文靠谱、哪些是噪音
* **为什么不自动推断？** 理想情况下系统应该自动判断 Agent 有没有用某条上下文（比如对比 Agent 的输出和上下文的内容）。但 ContextHub 作为后端中间件，无法直接获取 Agent 的生成输出。所以 Phase 3 先做显式反馈（Agent 主动报告），隐式自动推断留到后续增强。

2. **质量评分融入检索排序**：`quality_score = adopted_count / (adopted_count + ignored_count + 1)`，高采纳率上下文排名靠前，高忽略率噪音上下文被降权
* **通俗含义**：系统根据反馈信号给每条上下文算一个"质量分"。被 Agent 频繁采纳的上下文，质量分高，下次检索时排在前面；被反复忽略的上下文，质量分低，排名被压低——相当于"好评越多排名越高，差评越多排名越低"。
* **举例**：
  - 上下文 A 被检索了 20 次，其中 18 次被 Agent 采纳 → quality_score = 18/(18+2+1) ≈ 0.86（高质量）
  - 上下文 B 被检索了 20 次，其中只有 3 次被采纳 → quality_score = 3/(3+17+1) ≈ 0.14（噪音）
  - 下次有人搜索类似问题时，A 排在前面，B 被降权
  - 如果上下文 C 刚刚创建，只被检索了 1 次 → quality_score 趋近 0.5（数据不够，不急着惩罚也不急着奖励）

3. **低质量上下文管理**：通过报告识别和管理噪音上下文
* **通俗含义**：系统定期生成"低质量上下文报告"——列出那些被检索次数很高但采纳率很低的上下文（它们经常出现在搜索结果里，但 Agent 几乎不用它们，说明它们是噪音）。管理员可以根据报告清理或修正这些上下文。
* **举例**：
  - 周报显示：`ctx://team/memories/old-naming-convention` 被检索了 50 次，但只被采纳了 2 次（采纳率 4%）
  - 管理员检查后发现：这条上下文是过时的命名规范，内容已经不适用了，但因为关键词匹配度高，总是被检索出来
  - 管理员决定归档或更新这条上下文，减少后续的噪音干扰

#### 生命周期管理

4. **自动状态转换**：上下文按生命周期策略自动进行 `active → stale → archived → deleted` 状态转换
* **通俗含义**：随着时间推移，上下文会越来越多。如果不清理，系统里会堆满过时的、没人看的旧数据，拖慢检索质量和性能。Phase 3 实现自动化的"保质期"管理：管理员配置策略（比如"Agent 私有记忆 90 天没人访问就标为过时"），系统自动执行状态转换，不需要人工清理。
* **举例**：
  - 管理员配置策略：Agent 私有记忆超过 90 天未被访问 → 自动标为 `stale`（过时）
  - `stale` 状态持续 30 天仍无人访问 → 自动标为 `archived`（归档）
  - `archived` 状态持续 180 天 → 自动标为 `deleted`（逻辑删除）
  - 整个过程全自动，由后台定时任务扫描执行

5. **stale 恢复**：stale 上下文被直接访问（read）时自动恢复为 active
* **通俗含义**：如果一条上下文因为长期无人访问被自动标为"过时（stale）"，但某天有 Agent 又来读取它了，说明这条数据其实还有用。系统会自动把它恢复为"活跃（active）"状态，不需要管理员手动操作。这就像图书馆里，一本长期没人借的书被放到了"待下架"区，但有人又来借了，那就把它放回正常书架。
* **举例**：
  - `ctx://agent/query-bot/memories/cases/old-sql-trick` 已经 100 天没人访问，被自动标为 `stale`
  - 某天 analysis-agent 通过 URI 直接读取了这条上下文
  - 系统自动将它的状态从 `stale` 恢复为 `active`，`stale_at` 时间戳被清空，`last_accessed_at` 更新为当前时间

6. **变更传播与生命周期统一**：变更传播触发的 stale 标记与生命周期策略触发的 stale 标记共享同一状态机
* **通俗含义**：上下文变成 `stale` 有两种原因：（1）上游依赖发生变更（比如 Skill 发布了 breaking change，依赖它的 case 被标为过时）——这是 Phase 1 的变更传播；（2）长期无人访问被定时任务标为过时——这是 Phase 3 新增的生命周期策略。两种原因导致的 `stale` 共用同一个状态字段和同一套恢复逻辑，不会出现"两套过时标记互相打架"的问题。
* **举例**：
  - 一条 case 因为依赖的 Skill 发布了 breaking v3 而被标为 `stale`（变更传播触发）
  - 30 天后，生命周期定时任务扫描到它仍然是 `stale` 状态且无人访问 → 自动转为 `archived`
  - 如果在这 30 天内有 Agent 读取了它 → 无论是哪种原因导致的 stale，都会触发同一套恢复逻辑，恢复为 `active`

7. **归档管理**：archived 上下文从向量索引中移除（清除 `l0_embedding`），从搜索结果中排除，但仍可通过 URI 直接读取
* **通俗含义**：归档的上下文不再参与搜索——它的向量索引被清除，所以语义搜索和关键词搜索都不会命中它。但如果你知道它的 URI 地址，仍然可以直接读取它的内容。这就像把旧文件从搜索引擎里移除了，但如果有人手里有直链，还是能访问到。
* **举例**：
  - `ctx://team/memories/2025-q1-sales-pattern` 被归档了
  - Agent 搜索"销售模式"时，这条上下文**不会出现在搜索结果中**（因为向量索引已清除）
  - 但如果 Agent 通过 `read ctx://team/memories/2025-q1-sales-pattern` 直接访问，仍然能读到完整内容
  - 这样设计是因为归档的数据可能仍有历史参考价值，不应该完全不可访问

#### 长文档检索

8. **长文档入库**：文件系统存储 + PG 元数据的混合架构，支持 PDF + 纯文本 + Markdown
* **通俗含义**：Phase 1/2 处理的上下文都是 KB 级的短文本（一条记忆、一个 Skill 定义、一段表 schema），直接存在 PostgreSQL 里没问题。但企业里还有 MB 级的长文档（财报、技术手册、法规文件，动辄上百页）。把这些大文件塞进数据库不划算，所以 Phase 3 采用混合架构：原文存在文件系统（方便用 ripgrep 直接搜索），章节结构和摘要等元数据存在 PG 里（方便做结构化查询和树导航）。
* **举例**：
  - 入库一份 200 页的《企业数据治理规范 v3.pdf》
  - 系统做三件事：（1）PDF 原文存到文件系统目录 `{doc_store_root}/{uri_hash}/source.pdf`，同时提取纯文本版 `extracted.txt`；（2）在 `contexts` 表创建一行，`context_type='resource'`，`file_path` 指向文件路径，L0 存一句话摘要、L1 存目录概览；（3）在 `document_sections` 表创建章节树结构

9. **树导航检索**：`TreeRetriever` — PG 树节点 → LLM 逐层推理 → 文件系统读取，实现章节级精确定位
* **通俗含义**：对于长文档，传统的"切成小块 → 向量检索"的方式会丢失文档结构。树导航检索的思路完全不同：先利用文档本身的章节层级（目录树），让 LLM 从根节点开始逐层推理"用户的问题最可能在哪个章节"，一步步缩小范围直到定位到具体段落，然后再从文件系统读取原文。这就像人翻一本书：先看目录确定大章节，再翻到那个章节找小节标题，最后定位到具体段落。
* **举例**：
  - 用户问："数据治理规范里，关于敏感数据分类的标准是什么？"
  - 系统从章节树的根节点开始，LLM 判断："这个问题应该在'第三章 数据分类与分级'里"
  - 进入第三章的子节点，LLM 判断："应该在'3.2 敏感数据分类标准'小节"
  - 读取 3.2 小节的 start_offset ~ end_offset 范围内的原文，返回给 Agent
  - 全程只读了文档的一小段，而非把 200 页全部塞进 prompt

10. **关键词检索**：`KeywordRetriever` — ripgrep 搜索 + Monte Carlo 采样
* **通俗含义**：这是长文档的第二条检索路径，适合用户已经知道要找什么关键词的场景。直接用 ripgrep（一种极快的文件搜索工具）在文件系统里搜关键词，找到所有命中位置，然后用 Monte Carlo 采样从大量命中中提取最有价值的"证据窗口"（包含关键词的上下文片段）。这种方式不需要 LLM 推理，速度极快，适合精确关键词查找。
* **举例**：
  - 用户问："文档里提到 PII（个人可识别信息）的地方有哪些？"
  - ripgrep 在 `extracted.txt` 中搜索 `PII`，找到 37 处命中
  - 系统从 37 处命中中采样，提取若干最有代表性的"证据窗口"（每个窗口包含关键词前后各几百个字符的上下文）
  - 返回这些证据窗口给 Agent，无需读取整个文档

### 核心产出


| 产出                                    | 说明                                              |
| ------------------------------------- | ----------------------------------------------- |
| `context_feedback` 表                  | 反馈记录存储                                          |
| `lifecycle_policies` 表                | 生命周期策略配置                                        |
| `document_sections` 表                 | 长文档树结构                                          |
| FeedbackService                       | 反馈采集 + quality_score 计算 + RetrievalService 排序集成 |
| LifecycleService + LifecycleScheduler | 生命周期状态自动转换、stale 恢复、归档清理                        |
| LongDocumentIngester                  | 长文档入库（文件系统 + PG 元数据）                            |
| TreeRetriever / KeywordRetriever      | 长文档检索双路径                                        |
| RetrievalRouter                       | 检索策略路由，可插拔策略分发                                  |
| `contexthub_feedback` tool            | OpenClaw Plugin 新增工具                            |


---

## Phase 4 — ECMB 量化评估

**一句话**：构建可复现的量化评估框架（Enterprise Context Management Benchmark），通过 **Tier 1 硬指标**和 **Tier 2 A/B 消融实验**，量化 ContextHub 各核心设计决策的收益强度，将 MVP 从"能跑通"提升到"能量化证明收益有多大"。

### 具体目标

1. **实验 1 — L0/L1/L2 vs 平坦 RAG**：证明分层检索相比标准 chunk+vector RAG，Token 消耗显著降低（预期 60–80%），SQL 执行准确率（EX）持平或略高
2. **实验 2 — 有/无结构化关系**：证明结构化上下文组装（JOIN 关系 + 查询模板 + 业务术语）相比纯语义检索，在多表 JOIN 查询上 EX 显著提升
3. **实验 3 — 有/无变更传播**：证明变更传播引擎在 schema 变更后能维持下游 SQL 质量，而无传播时质量显著下降
4. **实验 4 — 有/无共享记忆**：证明跨 Agent 共享记忆（知识继承）相比冷启动，analysis-agent 的 EX 更高、首次正确回答所需轮次更少
5. **统计显著性**：所有对比 p < 0.05 或标注不足时诚实说明
6. **竞品对比**：与 Mem0 / CrewAI / Governed Memory 的功能维度对比表
7. **诚实标注**：每个实验的局限性、信号重叠和不可量化的能力

### 核心产出


| 产出                                     | 说明                                                      |
| -------------------------------------- | ------------------------------------------------------- |
| 测试数据集                                  | BIRD 子集 + 企业增强（JOIN 关系、血缘、查询模板、业务术语、变更场景、多 Agent 工作流数据） |
| BenchmarkRunner                        | 可复现实验执行框架 + ExperimentConfig                            |
| FlatRAGBaseline                        | 公平对照组——行业标准 RAG（chunk → embed → top-K → prompt）         |
| SQLGenerationPipeline                  | LLM 调用 + 上下文注入 + SQL 执行 + 结果比较                          |
| MetricsCollector + StatisticalAnalyzer | EX、Token per Query、Table Precision@5、延迟、统计显著性分析         |
| 评估报告                                   | 量化结果、统计分析、对比表、局限性标注                                     |


### 设计约束

- **EX（Execution Accuracy）** 为主指标：比较生成 SQL 在数据库上的执行结果而非 SQL 文本本身
- **Baseline 必须公平**：使用相同 LLM、相同 embedding 模型、相同 prompt 骨架、相同数据源
- **实验隔离**：每次运行前从 seed 脚本重建数据库状态，temperature=0 确保可复现

---

## Phase 5 — 生产加固

**一句话**：将 ContextHub 从"单实例可用"提升到"**多实例可部署、多框架可接入、真实数据源可对接**"的生产就绪状态。

### 具体目标

#### 多实例安全

1. **PropagationEngine SKIP LOCKED**：多个 ContextHub 实例并发处理 `change_events`，每个实例通过 `SELECT FOR UPDATE SKIP LOCKED` 领取不同事件子集，无重复、无遗漏
2. **LifecycleScheduler / ReconcilerService 排他执行**：PG advisory lock 实现多实例下的 sweep 去重，防止重复状态转换
3. **LISTEN/NOTIFY 多实例兼容**：任意一个实例收到通知即可触发处理

#### 多框架接入

1. **MCP Server**：暴露 ContextHub 全部 Agent 工具（ls/read/grep/stat/store/promote/skill_publish/feedback）和资源发现能力，符合 MCP 协议规范，支持 stdio + HTTP SSE 传输，打通非 OpenClaw Agent 框架接入通道

#### 真实数据源对接

1. **真实 Catalog 连接器**：连接器框架强化 + 至少一种真实连接器（如 Hive Metastore / Snowflake Information Schema）+ 健康检查与重试

#### 运维可观测性

1. **生产级可观测性**：结构化日志、Prometheus 指标导出、分层健康检查（liveness / readiness）

#### 技术债务收敛

1. **pool.py codec 收敛**：集成 `codecs.py`，消除废弃 API 风险
2. **性能回归基线**：复用 ECMB Benchmark 框架建立 CI 自动化性能回归检测

### 核心产出


| 产出                    | 说明                                           |
| --------------------- | -------------------------------------------- |
| SKIP LOCKED 事件领取      | PropagationEngine 多实例无重复处理                   |
| PG advisory lock 排他调度 | LifecycleScheduler / ReconcilerService 多实例安全 |
| MCP Server            | MCP 协议实现 + tools/resources 映射 + 双传输层         |
| 真实 Catalog 连接器        | 连接器框架 + 端到端 catalog sync                     |
| 可观测性套件                | 结构化日志 + Prometheus metrics + 健康检查            |
| 多实例集成测试               | MI-1 ~ MI-6 多实例正确性测试                         |


---

## 后续增强方向

以下能力已在 Phase 1–5 中被明确排除，但在 `plan/` 设计文档或 `14-adr-backlog-register.md` 中有记录。它们是 Phase 5 之后的潜在演进方向，按主题分类汇总。

### 权限与安全

| 条目 | 当前状态 | 重开前提 | owner 文档 |
|------|---------|---------|-----------|
| 写路径 ACL overlay | Phase 2 只覆盖 `read` action，写权限沿用 Phase 1 的 ownership + team_memberships | 出现需对写操作做细粒度 allow/deny 控制的场景，且已有统一的写路径语义模型 | `05-access-control-audit.md` |
| ACL principal 类型化 / 命名空间化 | `access_policies.principal` 复用裸字符串同时承载 `agent_id` 和 `team_path`，语义依赖二者命名不碰撞 | 出现 `agent_id` 与 `team_path` 的真实碰撞，或 Admin API / share grant 需要对 principal 做强类型校验 | `05-access-control-audit.md` |
| path ACL 升级到 ReBAC / Zanzibar | 当前 path ACL overlay 已覆盖已知需求 | path ACL 已无法表达跨对象关系授权，或策略数量 / 评估复杂度已失控 | `12-evolution-notes.md` ADR-C |
| 结构化字段脱敏策略精细化 | Phase 2 的 `sample_data` 精确 key 匹配已覆盖最高风险场景，`joins` / `top_templates` 保持原样不脱敏 | `joins` 值或 SQL 模板中出现需脱敏关键词并跨安全域传播；或 `sample_data` 敏感信息出现在复合列名 / 嵌套 JSON 值中 | `14-adr-backlog-register.md` |

### 反馈与生命周期

| 条目 | 当前状态 | 重开前提 | owner 文档 |
|------|---------|---------|-----------|
| 隐式反馈推断（afterTurn 自动判断 adopted / ignored） | Phase 3 只做显式反馈（Agent 主动调用 tool 报告） | 调研确认 OpenClaw afterTurn 可获取 Agent 完整输出 | `07-feedback-lifecycle.md` |
| 反馈驱动自动 promote | Phase 3 建立数据基线，不自动晋升 | 积累足够反馈数据验证 quality_score 可靠性后 | `07-feedback-lifecycle.md` |
| pending_review 审核流程 | MVP 跳过审核，记忆晋升直接 active | 出现需要人工审批记忆晋升的合规需求 | `00a-canonical-invariants.md` §5.1 |

### 检索增强

| 条目 | 当前状态 | 重开前提 | owner 文档 |
|------|---------|---------|-----------|
| CrossEncoder / LLM Rerank 策略 | MVP 只用 KeywordRerankStrategy (BM25)，接口已预留 | BM25 精排在特定场景下精度不足，需更高质量排序 | `02-information-model.md` |
| 混合检索策略（树导航 + ripgrep 联合） | Phase 3 先实现两条独立路径 | 两条路径独立验证效果后，需要联合提升召回率 | `11-long-document-retrieval.md` |
| 长文档跨文档关联检索 | Phase 3 先做单文档内章节定位 | 出现跨文档引用和关联定位的业务需求 | `11-long-document-retrieval.md` |
| 非 PDF 长文档格式支持（DOCX、HTML 等） | Phase 3 先支持 PDF + 纯文本 + Markdown | 出现其他格式的文档入库需求 | `11-long-document-retrieval.md` |

### 运维与部署

| 条目 | 当前状态 | 重开前提 | owner 文档 |
|------|---------|---------|-----------|
| LISTEN/NOTIFY 迁移到消息队列 | PG outbox + SKIP LOCKED 已足够生产级多实例 | outbox 补扫 / 领取成为瓶颈，或需要 dead-letter / replay 能力 | `12-evolution-notes.md` ADR-B |
| PG L2 迁移到对象存储 | PG-only 在一致性和复杂度上最优；长文档已采用 file-backed resource | 出现真实的 MB 级通用 `l2_content`，且 PG TOAST / VACUUM / 存储成本已成瓶颈 | `12-evolution-notes.md` ADR-A |
| Kubernetes Operator / Helm Chart | Phase 5 提供多实例能力但不绑定编排平台 | 需要标准化的 K8s 部署方案 | — |

### 评估与产品化

| 条目 | 当前状态 | 重开前提 | owner 文档 |
|------|---------|---------|-----------|
| run snapshot / context bundle | 当前复现依赖人工收集 URI 和版本 | 出现高频的调试复现、故障回放或跨 run handoff 需求 | `14-adr-backlog-register.md` |
| 端到端 Agent 对话质量评估 | Phase 4 以自动化 SQL Benchmark 为主 | 需要完整 OpenClaw 运行时 + 人工评估的端到端质量信号 | `09-implementation-plan.md` |
| 可视化评估报告 | Phase 4 以 Markdown 表格为主 | 需要交互式图表展示 Benchmark 结果 | — |
| 团队管理 CRUD API | MVP 阶段团队结构通过 seed data 预置 | 产品化阶段需要动态管理团队层级和成员关系 | `09-implementation-plan.md` |


