# 06 — AMC 的变更传播与反馈闭环

本章对齐主计划：
- `plan/06-change-propagation.md`
- `plan/07-feedback-lifecycle.md`

## 6.1 为什么 AMC 需要传播机制

轨迹不是静态知识。以下变更会导致历史轨迹失效：

- Skill 更新（参数规范变化）；
- 表结构变更（SQL 轨迹不可执行）；
- 工具行为变化（返回字段变化）；
- 共享轨迹被纠错（下游派生轨迹需同步）。

## 6.2 事件模型

```python
class AMCChangeEvent:
    event_id: str                 # 事件唯一 ID
    source_uri: str               # 变更源对象 URI
    source_type: str              # 源类型：skill | table | tool | trajectory
    change_type: str              # 变更类型：modified | deleted | version_published
    diff_summary: str | None      # 变更摘要（可选；MVP 可仅依赖 metadata）
    metadata: dict                # 结构化细节（如 is_breaking/old_version/new_version）
    occurred_at: datetime         # 事件发生时间
```

## 6.3 传播动作（当前仅 L1）

| Level | 动作 | Token 成本 | 场景 |
|------|------|------------|------|
| L1 | `mark_stale` | 0 | 明确 breaking 变更 |

当前版本只启用 `L1: mark_stale`：
- 变更事件命中规则后，将相关轨迹标记为 `stale`；
- retrieve 默认降权或不返回（可通过参数强制包含）；
- `stale` 标记写入 trajectory 元信息与图后端标签（raw/clean 图同步）。

后续扩展（暂不纳入当前实现）：
- `L2: auto_patch`（规则自动修补）；
- `L3: llm_revalidate`（模型语义复核）。

### 6.3.1 Skill 变更影响范围判定（反向依赖查询）

为避免全量扫描，AMC 维护两类依赖数据：
1. 轨迹本地声明：`ctx://.../trajectories/{trajectory_id}/.deps.json`
2. 反向依赖索引：`dep_uri -> [trajectory_id, dep_type, dep_version?]`

判定流程（当前 L1）：
1. 接收 skill 变更事件（`source_uri`, `is_breaking`, `new_version`）；
2. 用反向依赖索引查询 `dep_uri == source_uri` 的 trajectory 集合；
3. 若 `is_breaking=true`，命中轨迹全部执行 `mark_stale`；
4. 写审计日志与传播指标（命中数、耗时、标记成功率）。

说明：
- 当前阶段采用保守策略：breaking 即全量标记；
- 后续可基于 `dep_version`（可选字段）做精细化判定（仅标记受影响版本）。


## 6.4 反馈采集与回写

沿用 adopted / ignored / corrected / irrelevant：

```python
class TrajectoryFeedback:
    trajectory_id: str             # 反馈对应的轨迹 ID
    session_id: str                # 反馈所属会话 ID
    outcome: str                   # 反馈结果：adopted | ignored | corrected | irrelevant
    evidence: dict                 # 反馈证据（节点复用、纠错信号等）
    created_at: datetime           # 反馈写入时间
```

回写逻辑：
- 更新 trajectory 质量分；
- 更新 node/edge 的“可复用度”；
- 更新同主题/同工具骨架下的 workflow 候选权重。

## 6.5 生命周期策略（AMC 子域）

状态语义区分：
- `stale`：由变更传播触发的“可能失效/不兼容”**兼容性标记**（如 skill/schema 变化），与生命周期阶段正交；
- `cold`：由长期未命中触发的“低活跃”状态（不是错误或不兼容）。

状态机：

```
active -> cold -> archived -> deleted
   ^      |
   |------|
   被重新采用时恢复 active
```

说明：
- 生命周期状态机只描述 `active/cold/archived/deleted` 的活跃度演进；
- `stale` 作为独立标记可附着在任一生命周期状态上（通常在 `active/cold` 最常见）。

建议默认策略：
- private trajectory：90 天未命中 -> cold；再 60 天 -> archived；
- team-shared trajectory：仅因长期低质量或长期未命中进入 cold；
- workflow template：默认不自动删除，仅可归档。

## 6.6 可观测指标

- Stale Detection Rate
- Propagation Latency
- False Stale Rate
- Feedback Coverage
- Quality Score 与人工评估相关性

