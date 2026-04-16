# AMC（Agent Memory Core）实施规划索引

AMC 是 ContextHub 在 ToB 场景中的“轨迹记忆中枢”，负责两件核心事：

1. **commit**：接收上层 Agent 轨迹，解析为计算图并持久化；
2. **retrieve**：面向当前任务，融合语义相似与轨迹图相似，召回最相关历史轨迹。

存储约束（本版统一口径）：
- 轨迹计算图（raw/clean）统一存储在图后端（如 Neo4j）；
- 文件系统仅保留 trajectory-level 信息（pointer + `.abstract.md` + `.overview.md` 等）；
- 轨迹路径按 `account_id/scope/owner_space` 分层归档（并映射到 `ctx://{scope}/{owner_space}/...`）。

该规划以 `/home/qchenax/OpenViking/overview.md` 对 OpenViking 的理解为参考，重点吸收其“分层信息 + 可观测检索 + 生命周期”思想，但 AMC 本身按 ContextHub 需求独立设计。

术语速览：
- `raw graph`：保留失败/重试/分支的原始轨迹图；
- `clean graph`：用于主检索的清洗轨迹图；
- `stale`：变更传播触发的兼容性标记；
- `cold`：长期未命中的低活跃生命周期状态。

---

## 文档索引

| 文件 | 主题 | 关键内容 |
|------|------|----------|
| [01-scope-and-principles.md](01-scope-and-principles.md) | 目标与边界 | AMC 职责、非目标、设计原则、与 ContextHub 边界 |
| [02-trajectory-information-model.md](02-trajectory-information-model.md) | 轨迹信息模型 | 轨迹规范化、动作节点/依赖边、版本与元数据模型 |
| [03-commit-pipeline.md](03-commit-pipeline.md) | Commit 方案 | 解析、图构建、依赖抽取、索引写入、异常轨迹处理 |
| [04-retrieve-pipeline.md](04-retrieve-pipeline.md) | Retrieve 方案 | Query 解析、双路召回（RAG+Graph）、融合排序与返回格式 |
| [05-multitenancy-and-access-control.md](05-multitenancy-and-access-control.md) | 多租户与权限 | account/scope/owner_space 隔离、ACL、审计、字段脱敏 |
| [06-change-propagation-and-feedback.md](06-change-propagation-and-feedback.md) | 变更传播与反馈闭环 | ChangeEvent、过时标记、反馈回写与重排 |
| [07-workflow-abstraction.md](07-workflow-abstraction.md) | 通用工作流抽象 | 从多条具体轨迹提炼通用 workflow 的阶段方案 |
| [08-architecture.md](08-architecture.md) | AMC 架构 | 模块图、存储分层、关键服务职责 |
| [09-implementation-plan.md](09-implementation-plan.md) | 落地计划 | Phase 切分、里程碑、指标、风险与缓解 |
| [10-main-code-structure.md](10-main-code-structure.md) | 主代码结构草案 | 代码目录、模块职责、接口抽象与链路交互（总览） |
| [11-core-code-logic-examples.md](11-core-code-logic-examples.md) | 核心代码逻辑示意 | commit/retrieve/feedback 主流程与 API 路由示意 |
| [12-configuration-spec.md](12-configuration-spec.md) | 配置规范 | config.yaml 与 .env 格式、加载规则与校验建议 |
| [13-phase1-test-design.md](13-phase1-test-design.md) | Phase 1 测试设计 | 基于 `sample_traj` 的 commit 阶段用例分层与 M1 验收口径 |
| [14-mvp-workflow-demo-plan.md](14-mvp-workflow-demo-plan.md) | MVP 演示设计 | 将跨 Agent workflow 存储与检索融入视频脚本（D11-D14） |
| [15-promote-trajectory-to-team-plan.md](15-promote-trajectory-to-team-plan.md) | Promote 设计 | AMC 轨迹/工作流提升到 team 的接口、ACL、存储与测试方案 |
| [16-openclaw-plugin-integration-plan.md](16-openclaw-plugin-integration-plan.md) | OpenClaw 集成设计 | AMC 作为 context engine plugin 的分阶段接入与验收方案 |
| [17-batch-commit-plan.md](17-batch-commit-plan.md) | Batch Commit 设计 | 批量轨迹 commit 的 API、LLM 批处理、FS/Graph/Vector 批写与一致性策略 |

## 与主计划文档的映射

AMC 需要与既有 ContextHub 方案保持一致：

- 对齐 `plan/04-multi-agent-collaboration.md`：团队级共享、跨 Agent 可见性边界；
- 对齐 `plan/05-access-control-audit.md`：deny-override、审计日志、脱敏策略；
- 对齐 `plan/06-change-propagation.md`：依赖注册、STALE、事件驱动传播；
- 对齐 `plan/07-feedback-lifecycle.md`：adopted/ignored/corrected 信号与生命周期策略；
- 对齐 `plan/08-architecture.md`：服务分层与可插拔存储抽象；
- 对齐 `plan/09-implementation-plan.md`：分阶段推进与可量化评估。

## 依赖关系（AMC 内部）

```
01-scope-and-principles ──→ 02-trajectory-information-model ──→ 03-commit-pipeline
                                      │                                 │
                                      └─────────────────→ 04-retrieve-pipeline
                                                                  │
                      05-multitenancy-and-access-control ─────────┤
                                                                  │
                      06-change-propagation-and-feedback ─────────┤
                                                                  ▼
                                                   07-workflow-abstraction
                                                                  │
                                                                  ▼
                                                        08-architecture
                                                                  │
                                                                  ▼
                                                     09-implementation-plan
```

