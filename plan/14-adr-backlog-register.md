# 14 — ADR / Backlog Register

本文件是 Session 7 的收敛结果。它的职责不是继续扩展设计，而是关闭设计空间：把所有非当前主线项明确分流到 `回流主线`、`明确后置`、`保留 ADR`、`直接放弃` 四类，并给出重开条件与 owner 文档。

---

## 1. 分流规则（冻结）

1. 如果没有这个主题，当前 MVP 就不正确、已冻结语义会自相矛盾，或对外承诺无法成立，它必须 `回流主线`。
2. 如果它不影响当前正确性，但解决的是明确、可命名的未来问题，并且触发条件可观测，它属于 `明确后置` backlog。
3. 如果它本质上是替代架构或升级路径，当前没有排期，只需要记录“为什么现在不做、什么条件下重开”，它属于 `保留 ADR`。
4. 如果它既不影响当前正确性，也没有明确触发条件，只是在保留抽象可能性，它应 `直接放弃`。

## 2. 回流主线

当前为空。

含义：
- 前六场冻结的主线语义足以支撑当前 MVP 正确性与对外承诺。
- 任何未来条目在重开前，都不得回写主线文档为“默认将来会做”。

## 3. 明确后置 Backlog

| 条目 | 解决的问题 | 触发条件 | 最小闭环 | 当前不做的代价 | owner 文档 | 重开入口 |
|------|------------|----------|----------|----------------|------------|----------|
| 显式 ACL / 审计 / 窄范围 reference+ACL 共享 | 默认可见性 + `promote` 无法表达例外授权、deny、字段脱敏与审计追踪 | 出现明确的例外授权、敏感字段脱敏或合规审计需求；且不能靠调整团队路径或 `promote` 解决 | 落 `access_policies` 的 allow/deny/mask + `audit_log`；如需更窄共享，可在此基线上补 `reference + ACL`，但不得改写默认可见性 | 当前只能通过 `promote` 复制到共享路径，无法表达细粒度例外与合规留痕 | `05-access-control-audit.md` | `05-access-control-audit.md` |
| ACL principal 类型化 / 命名空间化 | `access_policies.principal` 当前复用裸字符串同时承载 `agent_id` 和 `team_path`，语义依赖二者命名不碰撞；一旦出现同名 agent/team，策略命中会产生歧义 | 出现 `agent_id` 与 `team_path` 的真实碰撞；或 Admin API / share grant 需要对 principal 做强类型校验、展示和编辑 | 为 principal 引入稳定的类型边界：例如新增 `principal_type`，或冻结并强制校验全局不相交的命名约束；并同步收敛 ACL 查询、Admin API、share grant 的写入与校验语义 | 当前 ACL 评估的 principal 解析存在隐含前提，语义不是严格自描述；后续扩展时容易把歧义传播到策略创建、共享授权和审计解释中 | `05-access-control-audit.md` | `05-access-control-audit.md` |
| 反馈闭环与生命周期管理 | 检索排序缺少质量信号，长期噪音与陈旧 context 缺少系统治理 | 出现可观测的低采纳率噪音、陈旧上下文堆积，或需要量化反馈来调优检索 | 先做显式反馈，再做 `lifecycle_policies` 驱动的 stale/archive | 当前排序优化依赖人工判断，归档清理靠手工或临时脚本 | `07-feedback-lifecycle.md` | `07-feedback-lifecycle.md` |
| ECMB 量化 benchmark | MVP 正确性已验证后，需要量化首个垂直载体上的收益强度 | Tier 3 正确性测试稳定通过，且需要对外展示“收益有多大”而不只是“能跑通” | 落 Tier 1/2 基准与 A/B 消融，围绕 EX、检索精度、token、延迟做量化 | 当前只有功能正确性与 demo，缺少收益量级证据 | `09-implementation-plan.md` | `09-implementation-plan.md` |
| 长文档高级检索扩展 | 通用 L0/L1/L2 检索不足以支持 MB 级、章节化、需精确引文的长文档 | 出现 100+ 页、MB 级资源文档，或业务要求章节级定位与证据窗口抽取 | 文件系统原文 + `resource` 子类型 + 至少一种精确定位策略（树导航或关键词窗口） | 长文档只能退化成通用 `resource` 处理，检索精度和 token 效率不可控 | `11-long-document-retrieval.md` | `11-long-document-retrieval.md` |
| run snapshot / context bundle | 需要稳定复现一次 Agent run、做跨 run handoff、或把解析后的 context/version 集合打包复用 | 出现高频的调试复现、故障回放、人工交接需求，且现有日志 + URI 无法稳定重现当时视图 | 不可变 run manifest，记录 resolved skill version、关键 context URI、检索命中与导出/导入格式 | 当前复现依赖人工收集 URI 和版本，debug 与 handoff 成本高 | `14-adr-backlog-register.md` | `14-adr-backlog-register.md` |
| MCP Server | 需要将 ContextHub 暴露给 OpenClaw 之外的 Agent 框架 | 出现第一个明确的非 OpenClaw 接入需求 | 用现有服务语义映射 MCP tools/resources，不重定义版本解析、ACL 或传播边界 | 当前只支持 SDK + OpenClaw Plugin 接入 | `14-adr-backlog-register.md` | `14-adr-backlog-register.md` |

## 4. 保留 ADR

这些条目是替代架构，不属于当前排期。保留它们的目的仅是记录“为什么现在不做”和“什么条件下重开”，而不是暗示主线未来一定迁移。

| 条目 | 当前拒绝原因 | 重开前提 | 需要重新验证的假设 | owner 文档 | 重开入口 |
|------|--------------|----------|--------------------|------------|----------|
| path ACL 升级到 ReBAC / Zanzibar | 当前路径层级 + 默认可见性 + path ACL overlay 已能覆盖已知需求；现在引入关系图授权会显著增加建模、查询与运维复杂度 | path ACL 已无法表达跨对象关系授权，或策略数量/评估复杂度已明显失控 | 关系图是否真的比路径模型更贴合对象关系；查询延迟预算是否允许；默认可见性是否仍是第一层基线 | `12-evolution-notes.md` | `05-access-control-audit.md` |
| LISTEN/NOTIFY 迁移到消息队列 | 当前 outbox 语义已冻结为 `change_events` source-of-truth，单实例/低规模下额外队列只会增加基础设施与语义混乱 | 传播引擎进入多实例部署，或 outbox 补扫/领取成为瓶颈，或需要 dead-letter / replay 等能力 | 队列是否只承担唤醒而非事件持久化；幂等边界是否保持不变；吞吐与恢复指标是否已超出 PG 能力 | `12-evolution-notes.md` | `06-change-propagation.md` |
| PG L2 迁移到对象存储 | 当前 PG-only 在一致性和实现复杂度上最优；过早引入对象存储会带来双写与补偿复杂度 | 出现真实的 MB 级内容或存储/VACUUM 成本压力，且已证明 PG-only 不再可接受 | 哪些 `context_type` 真的需要迁移；是否仍需事务性联动；长文档 file-backed `resource` 是否已经覆盖主要大文本需求 | `12-evolution-notes.md` | `01-storage-paradigm.md` |

## 5. 直接放弃

这些条目不再保留为 roadmap 或隐含设计空间。若未来真的重提，必须以一个全新的、带触发条件的问题重新立项，而不是引用旧的“也许以后要做”描述。

| 条目 | 拒绝原因 | 清理要求 |
|------|----------|----------|
| 自动依赖捕获替代写入路径显式登记 | 依赖传播的正确性必须来自写入路径的确定性建边；离线扫描只能做诊断/补洞建议，不能成为 canonical 依赖语义 | `06-change-propagation.md` 不得再出现“高置信度自动补边”或“扫描替代显式登记”的表述 |
| “文件作为入口”的导入范式作为当前 roadmap 主题 | 这是一个模糊方向，不影响当前正确性，也没有明确触发条件；暂不保留为独立设计议题 | `13-related-works.md` 仅保留参考价值，不再作为待探索路线 |
| `pg_cron` 作为架构级路线项 | 这是局部实现选择，不是产品语义或架构边界；是否采用可在实现时按部署条件决定 | `09-implementation-plan.md`、`13-related-works.md` 不再把它写成 roadmap 条目 |
| PG 环境快照 / branch 作为产品路线项 | 这是评估执行细节，不是产品能力；如未来做 ECMB，可作为实验方法临时采用 | `13-related-works.md` 只保留为 benchmark 执行备注，不单列 future 主题 |

## 6. 清理约束

从 Session 7 起，主线文档遵循以下规则：

- 主线文档只写当前要实现的语义、机制和边界。
- 需要后置的主题，写“边界 + 指向 owner 文档 / register”，不再写开放式 TODO。
- ADR 文档只记录替代架构的拒绝原因与重开条件，不预写实现草图到主线。
- `related works` 只做参考分析，不再携带未分流的产品路线项。
