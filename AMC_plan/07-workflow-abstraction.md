# 07 — 从具体轨迹到通用 Workflow 的抽象方案

这是 AMC 的下一阶段能力：将多个同类任务轨迹聚合为可复用“通用工作流”。

## 7.1 目标

给定同类任务（例如“sales analysis”）的 N 条轨迹，输出：

- 通用步骤骨架（阶段序列）；
- 每阶段推荐工具与参数模板；
- 常见失败分支与修复策略；
- 适用前提与不适用边界。

## 7.2 输入与产出

### 输入
- 一组高质量轨迹（按 task_type 或标签筛选）；
- 每条轨迹的反馈分与执行结果；
- 可选专家标注（关键步骤、禁止步骤）。

说明：
- 轨迹入口从 `ctx://{scope}/{owner_space}/memories/trajectories/...` 读取；
- 节点/边结构通过 `graph_pointer` 到图后端（如 Neo4j）拉取。

### 输出
```python
class WorkflowTemplate:
    workflow_id: str                   # 模板唯一 ID（版本管理主键）
    account_id: str                    # 账户隔离字段
    name: str                          # 模板名称
    task_type: str                     # 适用任务类型（retrieve 过滤字段）
    stages: list[WorkflowStage]        # 主流程阶段定义（工具链骨架）
    failure_playbook: list[FailurePattern]  # 常见失败模式与修复策略
    confidence: float                  # 模板可信度（覆盖率/成功率/反馈综合）
    source_trajectories: list[str]     # 来源轨迹 ID 列表（可审计）
```

## 7.3 抽象方法（分阶段）

### Phase A：规则聚合（MVP+1）
- 按工具序列 + 关键节点语义聚类；
- 提取频繁子路径（frequent subpath mining）；
- 生成“主路径 + 分支路径”。

### Phase B：图模式挖掘
- 对轨迹图做 motif mining；
- 对齐不同轨迹中的同构子图；
- 提炼通用依赖关系（不仅是顺序，还包括数据依赖）。

### Phase C：LLM 辅助泛化
- 将候选路径转成可读 workflow 描述；
- 生成参数模板和检查清单；
- 由人审后发布到 team/datalake scope。

## 7.4 质量门槛

仅当满足以下条件才允许发布 Workflow：

- 来源轨迹数量 >= M（如 10）；
- 覆盖率 >= C（如该类任务 60% 可套用）；
- 失败率不高于基线；
- 人工审阅通过（至少一名领域负责人）。

## 7.5 与 Retrieve 的联动

retrieve 可先召回 workflow template，再补充具体轨迹证据：

```
Workflow first -> trajectory evidence second
```

这样可同时给上层 Agent：
- 可复用“骨架”（高层策略）
- 可复制“片段”（底层操作细节）

## 7.6 风险与控制

- 过度泛化：模板看似通用但忽略边界；
- 误学习：把错误操作固化为模板；
- 版本漂移：底层 Skill/表结构变化导致模板失效。

控制手段：版本化、STALE 传播、周期重评估、人工门禁。

