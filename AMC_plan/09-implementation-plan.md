# 09 — AMC 落地实施计划（commit/retrieve 优先）

## 9.1 MVP 验证场景

场景：数据分析 Agent 的 SQL 任务（与 `sample_traj` 相同类型）

1. Agent 执行任务并持续提交轨迹（commit）；
2. 新任务到来时，携带 task 描述与 partial trajectory 调用 retrieve；
3. AMC 返回历史高相似轨迹片段（含失败修复链）；
4. 观察是否提升任务完成率、降低无效尝试。

术语说明：
- `里程碑 M1/M2/...`：阶段验收检查点，不代表独立发布版本号；
- `Failure-Fix Hit Rate`：检索结果中命中“失败->修复”可复用片段的比例；
- `Isolation Violation Rate`：越权可见/可操作事件占比（目标 0）。

## 9.2 分阶段计划

### Phase 1：基础数据模型 + Commit 可用

1. 定义 trajectory/node/edge schema（见 02）；
2. 实现 commit API + 校验 + 幂等（默认关闭幂等，支持显式开启去重）；
3. 实现图构建器（raw/clean 双版本，LLM 抽取 dataflow/reasoning + rule-based/temporal fallback，含 retry 链）；
4. 对接 Graph Store（Neo4j）并写入 `graph_pointer` 到 `ctx://agent/{agent_id}/memories/trajectories/`；
5. 仅生成 trajectory-level `.abstract/.overview` 并建立向量索引（L0/L1 优先 LLM 生成，失败回退 rule-based；
   向量索引以 `uri` 对应文件，不直接存正文，embedding 时按 `uri` 回 FS 读取；
   重复 commit 时若原文变化需更新对应 embedding）；
6. 接入审计日志。

**里程碑 M1**：可稳定提交并查询单条轨迹详情，图构建成功率 > 95%（样例集）。

### Phase 2：Retrieve 双路召回

7. 实现语义召回（trajectory-level L0/L1 向量检索）；
8. 实现图特征抽取与近似匹配（仅当传入 `partial_trajectory` 时启用）：
   - 8.1 构建 query graph（raw/clean，默认 clean）；
   - 8.2 定义 MCS 匹配规则（节点按 action 函数名相同，边按 edge_type 相同；不要求 args/output/其他属性一致）；
   - 8.3 两阶段匹配（语义 top-N 候选收缩 -> MCS 图相似精排）；
   - 8.4 输出可解释 evidence（MCS 命中节点/边规模、命中规则、graph_score）；
   - 8.5 降级兜底（QG 构建失败或 Neo4j 不可用时回退语义召回）。
9. 实现融合 rerank（默认 clean graph，错误场景补充 raw graph）；
10. 返回 evidence（命中节点/子图说明）；
11. 接入 ACL 过滤与脱敏。

**里程碑 M2**：在样例任务上，Trajectory Recall@5 达到基线目标，检索可解释。

### Phase 3：反馈闭环 + 传播机制

12. 接收 adopted/ignored/corrected 反馈；
13. 反馈回写质量分并影响 rerank；
14. 接入 ChangeEvent 与 `L1: mark_stale`（当前不实现 L2/L3）；
15. 生命周期最小实现：`lifecycle_status`（active/cold/archived/deleted）+ `stale_flag`（独立兼容性标记）。

**里程碑 M3**：检索质量可随反馈迭代提升，过时轨迹可自动降权。

### Phase 4：Workflow 抽象（预研 + 最小产物）

16. 同类轨迹聚类与频繁子路径提取；
17. 输出 workflow 草案（人工可审）；
18. 发布到 team scope 并支持 retrieve 优先命中。

**里程碑 M4**：至少 1 个 task_type 产出可复用 workflow 模板。

## 9.3 验证指标（AMC 专项）

| 类别 | 指标 | 目标方向 |
|------|------|----------|
| Commit | Graph Build Success Rate | 越高越好 |
| Commit | Parse Error Rate | 越低越好 |
| Retrieve | Trajectory Recall@K | 越高越好 |
| Retrieve | Failure-Fix Hit Rate | 越高越好 |
| Retrieve | Context Adoption Rate | 越高越好 |
| 安全 | Isolation Violation Rate | 必须为 0 |
| 传播 | Stale Detection Rate | 越高越好 |
| 效率 | commit/retrieve P50/P99 | 越低越好 |

## 9.4 字段脱敏实现基线（非分阶段）

脱敏能力按“基线要求”统一实现，不再拆分阶段：

1. 实现 `FieldMaskPolicy`（principal/resource_pattern/actions/field_paths/mask_type/priority/version）；
2. Retrieve 链路固定执行：ACL -> 读取轨迹与图后端节点片段 -> 策略匹配 -> Mask Engine -> 返回；
3. 默认支持 `full | partial | hash | redact_regex` 四类脱敏；
4. 默认覆盖 `tool_args`、`tool_output`、`report.content` 三类敏感面；
5. 审计日志必须记录 `policy_version`、`masked_fields_count`、`masked_paths`。

字段脱敏验收口径：
- 敏感字段泄露率 = 0；
- 越权访问拦截率 = 100%；
- 脱敏后 retrieve 额外延迟 P95 < 30ms；
- 审计日志策略命中记录覆盖率 = 100%。

## 9.5 风险与缓解

1. **轨迹格式异构严重**  
   缓解：先定义 adapter 层；最小必填字段不足时走 raw fallback。

2. **依赖抽取不稳定**  
   缓解：规则 + 置信度分层；低置信边不用于高权重 graph score。

3. **图检索性能压力**  
   缓解：离线提取图特征并建立 ANN 索引，在线只做轻量匹配。

4. **反馈噪音大**  
   缓解：引入最小样本门槛与时间衰减，避免短期噪音主导排序。

5. **共享引入数据泄露风险**  
   缓解：默认私有，提升共享必须走 ACL + 审计 + 脱敏流程。

## 9.6 与主计划的集成检查清单

- [ ] 对齐 `plan/04` 的团队共享语义与提升流程  
- [ ] 对齐 `plan/05` 的 deny-override 与审计结构  
- [ ] 对齐 `plan/06` 的 ChangeEvent 与 `L1: mark_stale` 机制  
- [ ] 对齐 `plan/07` 的 feedback outcome 与 `lifecycle_status + stale_flag` 策略  
- [ ] 对齐 `plan/08` 的服务分层与存储抽象接口  
- [ ] 对齐 `plan/09` 的 phase 节奏与 benchmark 评估方法

