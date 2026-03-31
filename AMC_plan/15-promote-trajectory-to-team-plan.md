# 15 — AMC Promote 到 Team 实现方案（对齐 main memory/promote）

## 15.1 目标与范围

目标：在 AMC 中新增一版“将 agent 私有 trajectory/workflow 提升到所属 team 空间”的能力，行为口径尽量对齐 main 分支 `POST /api/v1/memories/promote`。

范围（本期）：
- 支持 `scope=agent -> scope=team` 的 promote；
- 仅支持提升“当前调用 agent 自己提交的轨迹”；
- 提升后可被团队成员在 retrieve 中召回；
- 记录 `derived_from` 血缘与审计日志。

非范围（后续）：
- team -> datalake promote；
- 跨 account promote；
- 基于审批流的延迟生效。

---

## 15.2 接口设计（AMC）

建议新增路由：

`POST /api/v1/amc/promote`

请求头（与 main 对齐）：
- `X-Account-Id`
- `X-Agent-Id`

请求体（建议）：

```json
{
  "trajectory_id": "traj_xxx",
  "target_team": "engineering",
  "reason": "promote reusable workflow"
}
```

返回体（建议）：

```json
{
  "source_uri": "ctx://agent/query-agent/memories/trajectories/traj_xxx",
  "target_uri": "ctx://team/engineering/memories/trajectories/traj_xxx",
  "scope": "team",
  "owner_space": "engineering",
  "derived_from": "ctx://agent/query-agent/memories/trajectories/traj_xxx",
  "status": "promoted"
}
```

---

## 15.3 处理流程（模仿 main 的校验顺序）

> 下面顺序直接对齐 main 的 `memory_service.promote`：先读源、再类型/所有权校验、再 ACL、再生成目标、再写依赖与审计。

1. **读取源 trajectory 元信息**  
   - 依据 `account_id + trajectory_id` 查 AMC 轨迹元信息（FS pointer / graph meta / vector meta 均可作为来源）；  
   - 若不存在或已删除，返回 `404`。

2. **类型校验**  
   - 仅允许 `context_type=trajectory`（或 `amc_trajectory`）；  
   - 否则 `400`。

3. **所有权校验（关键）**  
   - 源必须是 `scope=agent` 且 `owner_space == X-Agent-Id`；  
   - 否则 `403`。  
   - 含义：只能提升“我自己的私有轨迹”。

4. **目标写权限校验（ACL）**  
   - 调 ACL 的 team 写权限检查：`check_write_target(scope=team, owner_space=target_team)`；  
   - 无 `read_write` 权限则 `403`。

5. **构造目标 URI 与目标作用域**  
   - `target_uri = ctx://team/{target_team}/memories/trajectories/{trajectory_id}`  
   - `scope=team`，`owner_space=target_team`。

6. **写入 promote 后的“team 视图”记录（幂等/冲突处理）**  
   建议与 main 一样“插入新记录，而非就地改源记录”，并保持源记录不变。  
   - 若已存在相同 `target_uri`：返回 `409`（或按参数支持幂等返回 existing）。

7. **写血缘关系 `derived_from`**  
   - 在 dependencies / graph edge 中写一条 `team_trajectory -> source_agent_trajectory`；
   - 便于审计与后续 stale 传播。

8. **写审计日志与变更事件**  
   - `action=promote_trajectory`；  
   - metadata 至少含：`source_uri`、`target_uri`、`target_team`、`actor`。

9. **索引一致性更新**  
   - 让 `retrieve` 可以命中 team 记录：  
     - 向量层新增（或复制）一条 team scope 索引记录（`account_id/scope/owner_space/status` 过滤字段必须正确）；  
     - 图层补 team 视图节点或可查询映射；  
     - FS 写入 team 路径下最小 pointer（或 registry 映射），避免重拷贝大文件。

---

## 15.4 存储层建议（AMC 语义）

为兼顾可追溯和低成本，推荐“轻拷贝”策略：

1. **源数据不变**：agent 私有轨迹继续保留；
2. **team 侧新增一条 promote 记录**：只存元信息 + pointer；
3. **raw/clean 图不物理复制**：team 记录通过 pointer 引用同一图资产；
4. **向量索引可复制文档条目**：以新的 team uri 建索引（embedding 可复用，避免重复计算）。

这样满足：
- ACL 与 scope 查询语义清晰；
- 召回可直接按 team scope 命中；
- 存储成本可控。

---

## 15.5 错误码与行为约定

- `404 Not Found`：source trajectory 不存在或不可用；
- `400 Bad Request`：非 trajectory 类型、目标参数非法；
- `403 Forbidden`：非本人私有轨迹、或对 target_team 无写权限；
- `409 Conflict`：target uri 已存在；
- `201 Created`：promote 成功。

---

## 15.6 与 retrieve/ACL 的联动要求

promote 实现后，retrieve 必须满足：

1. SQL 主过滤可命中 team promoted 记录（`account_id/scope/owner_space/status`）；
2. 应用层兜底过滤仍保留；
3. 最终 ACL `filter_visible` 后，只有 team 成员可见；
4. 非成员 agent 召回不到该 workflow（越权返回率=0）。

---

## 15.7 测试计划（新增）

### 单元测试

1. promote 成功：agent 私有 -> team；
2. source 不存在 -> 404；
3. source 非 agent 私有 -> 403；
4. 无 team 写权限 -> 403；
5. 重复 promote -> 409。

### 集成测试

1. Agent A commit 后 promote 到 `team=engineering`；
2. Agent B（engineering 成员）retrieve 命中 promoted workflow；
3. Agent C（非成员）retrieve 不命中；
4. 审计日志包含 `promoted_from/source_uri/target_uri/target_team`。

---

## 15.8 分阶段落地建议

1. **P1（最小可用）**：API + ACL + promote registry + 审计；
2. **P2（检索打通）**：向量/图索引 team 视图写入；
3. **P3（一致性增强）**：derived_from 触发 stale 传播、支持 promote 幂等返回。

验收口径：
- 团队内可复用命中率提升；
- 越权可见率 = 0；
- promote 接口 P95 延迟可控（目标 < 300ms，不含重建 embedding）。
