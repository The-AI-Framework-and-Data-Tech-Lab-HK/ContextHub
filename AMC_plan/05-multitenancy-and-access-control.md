# 05 — AMC 的多租户、多 Agent 与访问控制

本章对齐主计划：
- `plan/04-multi-agent-collaboration.md`
- `plan/05-access-control-audit.md`

## 5.1 资源作用域模型

| Scope | 路径示例 | 可见性 | 典型用途 |
|------|----------|--------|----------|
| Agent 私有 | `ctx://agent/{agent_id}/memories/trajectories/...` | 仅 agent 本身 | 原始任务轨迹 |
| Team 共享 | `ctx://team/{team_path}/memories/trajectories/...` | 团队成员+上级继承 | 复用案例、共识模式 |
| 组织公共 | `ctx://team/memories/trajectories/...` | 全组织 | 标准工作流模板 |

默认写入 Agent 私有域；共享需提升流程（审批或策略自动化）。

## 5.2 ACL 评估规则

沿用主计划的 deny-override：

1. 显式 deny 优先；
2. allow 冲突取更高优先级；
3. 无策略默认 deny。

并增加 AMC 特有 action：
- `commit`
- `retrieve`
- `promote_trajectory`
- `promote_workflow`
- `replay`

## 5.3 字段脱敏（轨迹场景）

脱敏对象：
- tool_args 中敏感字段（如密钥、账号、手机号）；
- tool_result 中 PII/业务敏感字段；
- report 内容中的敏感文本片段。

策略：
- 存储层保留原文（便于审计与重算）；
- 检索返回时按调用方身份动态脱敏；
- 脱敏规则版本化，便于追踪“当时为何可见/不可见”。
- 图后端查询同样受 ACL 约束（先鉴权再读图）。

### 5.3.1 具体脱敏实施计划（MVP -> 增强）

#### A. 策略模型（先做）

```python
class FieldMaskPolicy:
    policy_id: str                # 策略唯一 ID
    version: int                  # 策略版本号（便于审计与回滚）
    principal: str                # 作用主体：role | team_path | agent_id
    resource_pattern: str         # 资源匹配表达（如 ctx://agent/*/memories/trajectories/*）
    actions: list[str]            # 生效动作列表（retrieve | replay）
    field_paths: list[str]        # 脱敏字段路径（JSONPath，如 $.tool_args.api_key）
    mask_type: str                # 脱敏方式：full | partial | hash | redact_regex
    effect: str                   # 生效效果：mask | deny
    priority: int                 # 冲突处理优先级（越大优先）
```

建议默认内置策略：
- `$.tool_args.*key*` -> `full`
- `$.tool_args.*token*` -> `full`
- `$.tool_output.data[*].phone` -> `partial`
- `$.tool_output.data[*].email` -> `redact_regex`
- `$.report.content` 中命中敏感正则片段 -> `redact_regex`

#### B. 执行链路（MVP 必做）

```
Retrieve Request
  -> ACL 鉴权（是否可访问 trajectory）
  -> 读取文件系统 trajectory-level 信息 + 图后端节点片段
  -> 策略匹配（principal + resource_pattern + action）
  -> Mask Engine 按 field_paths 脱敏
  -> 返回脱敏结果
  -> 写审计日志（命中的策略版本与字段）
```

说明：MVP 统一在应用层做脱敏，不在图查询层做字段裁剪，保证行为一致。

#### C. 验收标准

- 敏感字段泄露率 = 0（基于测试样本）；
- 越权访问拦截率 = 100%；
- 脱敏后 retrieve 额外延迟 P95 < 30ms；
- 审计日志完整记录策略命中信息（覆盖率 100%）。

## 5.4 审计日志扩展

```python
class AMCAuditEntry:
    timestamp: datetime           # 审计事件时间
    account_id: str               # 账户 ID
    actor: str                    # 操作主体（agent_id/user_id/system）
    action: str                   # 动作类型：commit | retrieve | promote | replay
    target_uri: str               # 主目标资源 URI
    query_hash: str | None        # 查询指纹（避免记录明文查询）
    retrieved_items: list[str]    # 命中项列表（用于追溯）
    result: str                   # 执行结果：success | denied | error
    metadata: dict                # 扩展信息（policy_version/masked_fields_count/error_code）
```

## 5.5 多 Agent 协作策略

- Agent A 的轨迹默认不对 Agent B 可见；
- 通过 `promote_trajectory` 将 A 的高质量轨迹提升到 team scope；
- 被提升轨迹需带：
  - 方法骨架/适用场景标签；
  - 适用边界（applicability）；
  - 风险提示（known pitfalls）。

## 5.6 隔离性测试要求

- Isolation Violation Rate = 0；
- “跨 account query 注入”必须返回空；
- 同 account 不同 team 的越权访问必须被拒绝并记审计。

