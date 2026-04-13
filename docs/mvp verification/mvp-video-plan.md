#### DEMO：多 Agent 协作全景（10 步）

> 本 demo 是独立的完整演示流程，一次性覆盖三个核心能力：
>
> | 能力 | 含义 | 对应步骤 |
> |------|------|----------|
> | 跨 Agent 上下文晋升 | agent A promote → agent B 可见 | D1-D2, D7 |
> | 私有空间隔离 | 各 agent 的私有记忆互不可见 | D3-D4, D5-D6 |
> | 双向协作共享 | 两个 agent 都向共享空间贡献知识 | D8-D9, D10 |

##### 故事背景(演示场景)

一家电商公司正在筹备春季促销。运营负责人拟定了活动规则（满 300 减 50、
叠加规则、活动档期），数据分析师则从历史用户行为中发现了一个关键洞察：
周末晚间 20:00-22:00 是下单高峰，如果在 19:30 推送促销通知，转化率
最高。

两人分属不同部门，但同在一个项目组中协作。最终，运营负责人结合自己制定
的活动规则和数据分析师提供的推送时间建议，制定出了完整的促销执行方案：
**"4 月 1-15 日，满 300 减 50，每周六 19:30 推送。"**

以下 demo 展示了这个方案从各自积累、到知识共享、再到协作汇聚的完整过程。
同时，每个人都有不该被对方看到的敏感信息（供应商谈判底价、未经验证的
A/B 测试数据），demo 也会验证这些信息确实被隔离保护。

##### 角色与验证能力映射

| 系统标识 | 业务角色 | 职责 |
|----------|----------|------|
| query-agent | 运营负责人 | 策划活动规则、对接供应商 |
| analysis-agent | 数据分析师 | 分析用户行为、提供数据洞察 |
| engineering 团队 | 项目组共享空间 | 两人协作的公共知识库 |

| 验证能力 | 含义 | 对应步骤 |
|----------|------|----------|
| 跨 Agent 上下文晋升 | agent A promote → agent B 可见 | D1-D2, D7 |
| 私有空间隔离 | 各 agent 的私有记忆互不可见 | D3-D4, D5-D6 |
| 双向协作共享 | 两个 agent 都向共享空间贡献知识 | D8-D9, D10 |

> 系统中的 agent ID 和 team name 是技术标识，不影响演示故事。

**启动状态**：clean session（建议在 clean DB 上执行，避免旧数据干扰）。
Sidecar 以 `--agent-id query-agent` 运行在 :9100。

```bash
# Terminal 3
python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000 \
  --agent-id query-agent --account-id acme
```

启动 Gateway 和 TUI：

```bash
# Terminal 4
pnpm openclaw gateway

# Terminal 5
pnpm openclaw tui
```

##### Part 1：运营负责人（query-agent）存储、晋升与私有保留

**Step D1 — 存储活动规则（准备晋升到团队）**

在 TUI 中输入：

```
请记住：春季促销活动规则 —— 满 300 减 50，可与会员折扣叠加，
不可与新人专享券同时使用。活动时间 4 月 1 日至 15 日。
```
<img width="1542" height="456" alt="image" src="https://github.com/user-attachments/assets/00b400f2-d7c3-472c-86e1-3fa10ec3af41" />

观察 Terminal 3 sidecar 日志出现 `dispatch contexthub_store` 调用。


**Step D2 — 晋升到团队共享空间**

在 TUI 中输入：

```
请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
```

<img width="1552" height="250" alt="image" src="https://github.com/user-attachments/assets/89680dba-1777-48bb-9805-fb7e1345b8d2" />


观察 sidecar 日志出现 `dispatch contexthub_promote` 调用。

> **提示**：如果 agent 没有主动调用工具，可以更直接地说：
> "请调用 contexthub_promote，把 URI ctx://agent/query-agent/memories/xxx
> 晋升到 engineering"（URI 从 Step D1 的 sidecar 日志中复制）。

**Step D3 — 存储一条敏感的私有备忘（不晋升）**

在 TUI 中输入：

```
请再记住一条：供应商谈判备忘 —— 春季促销的供货底价不能低于零售价的
60%，这个底线不要对外透露。这条只留在我的私有空间，不要共享。
```

<img width="1572" height="276" alt="image" src="https://github.com/user-attachments/assets/bdba01f5-46fd-42fa-b9c5-0d0acbdbc856" />


观察 sidecar 日志出现 `dispatch contexthub_store`，但**不会**出现
`contexthub_promote`。

**Step D4 — 验证运营负责人的私有空间**

在 TUI 中输入：

```
请列出我的私有空间的所有记忆
```

<img width="1852" height="488" alt="image" src="https://github.com/user-attachments/assets/f0a2fcc9-bfa8-4b5c-9a79-bfa44f70cf20" />

预期：列表中包含两条记忆 —— Step D1 的促销活动规则和 Step D3 的
供应商谈判底价。这为后续的隔离验证提供了对照基线。

##### 切换到 analysis-agent

在 TUI 中按 `Ctrl+C` 退出 → Terminal 4 `Ctrl+C` 停 Gateway →
Terminal 3 `Ctrl+C` 停 Sidecar。

重启 Sidecar，换 agent-id：

```bash
# Terminal 3
python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000 \
  --agent-id analysis-agent --account-id acme
```

重启 Gateway 和 TUI：

```bash
# Terminal 4
pnpm openclaw gateway

# Terminal 5
pnpm openclaw tui
```

##### Part 2：数据分析师（analysis-agent）隔离验证 + 协作贡献

**Step D5 — 数据分析师存储自己的私有记忆**

在 TUI 中输入：

```
请记住：上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率
比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
```

<img width="1888" height="286" alt="image" src="https://github.com/user-attachments/assets/fb2c94a4-4114-46d3-8a83-0456e3c41b5e" />

观察 sidecar 日志出现 `dispatch contexthub_store`。

**Step D6 — 验证隔离：数据分析师的私有空间不包含运营负责人的记忆**

在 TUI 中输入：

```
请列出我的私有空间的所有记忆
```

<img width="1900" height="346" alt="image" src="https://github.com/user-attachments/assets/bd14ff32-fe8f-4aff-8bd2-6c0754dfcd8a" />


预期：
- **只包含** Step D5 刚存的 A/B 测试初步结果
- **不包含** 运营负责人的"供应商谈判底价"记忆（Step D3）

> **这是私有隔离的关键证据**：运营负责人在 Step D4 中看到两条私有
> 记忆，而数据分析师只能看到自己的那一条。两个 agent 的私有空间
> 完全独立，互不干扰 —— 敏感的谈判底价不会泄漏给其他角色。

**Step D7 — 验证共享：数据分析师能看到运营负责人晋升的活动规则**

在 TUI 中输入：

```
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

<img width="1706" height="392" alt="image" src="https://github.com/user-attachments/assets/5f11fee7-51c2-4fbe-9247-8237a4b08340" />


预期：列表中包含运营负责人在 Step D2 晋升的春季促销规则 —— 这
证明跨 Agent 的上下文晋升在 runtime 中生效。

**Step D8 — 数据分析师也向共享空间贡献自己的洞察**

在 TUI 中输入：

```
请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00
是下单高峰期，建议将促销推送时间安排在 19:30。
然后把这条晋升到团队共享空间 engineering。
```

<img width="1662" height="468" alt="image" src="https://github.com/user-attachments/assets/1ae15bce-8296-47e0-81f0-9a88f5e6c8dc" />


观察 sidecar 日志依次出现 `dispatch contexthub_store` 和
`dispatch contexthub_promote`。

> **提示**：如果 agent 没有一次性完成存储和晋升，可以分两步引导，
> 或直接指定 URI："请调用 contexthub_promote，把 URI
> ctx://agent/analysis-agent/memories/xxx 晋升到 engineering"。

**Step D9 — 验证协作成果：共享空间包含两个角色的贡献**

在 TUI 中输入：

```
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

<img width="1598" height="410" alt="image" src="https://github.com/user-attachments/assets/fe4679e0-8454-49dd-8874-f8d6dbe6499a" />


预期：
- 包含运营负责人晋升的**春季促销规则**（来自 Step D2）
- 包含数据分析师晋升的**促销推送时间建议**（来自 Step D8）
- 两个不同角色的知识在同一个共享空间中共存

> **这是协作的关键证据**：共享空间不是单一角色的"导出"，而是多个
> 角色各自贡献、共同构建的知识库。运营带来的是业务规则，数据分析
> 带来的是用户洞察，合在一起才是完整的决策依据。

##### （可选）切换回 query-agent 验证双向可见

重新切换为 query-agent（同上切换流程：停 TUI/Gateway/Sidecar →
重启为 query-agent → 启动 Gateway/TUI）。

**Step D10 — 运营负责人确认能看到数据分析师的共享贡献**

在 TUI 中输入：

```
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

<img width="2010" height="450" alt="image" src="https://github.com/user-attachments/assets/5d49284b-0e50-4053-b8ce-00b500241adc" />


预期：
- 运营负责人也能看到数据分析师在 Step D8 晋升的促销推送时间建议
- 同时运营负责人的私有供应商谈判底价（Step D3）**仍然不会**出现在
  共享空间中 —— 只有主动晋升的内容才会共享
- 证明共享空间的修改是**双向生效**的：无论谁晋升，所有项目组成员都能看到

##### Phase D 验证要点总结

| 验证点 | 对应步骤 | 预期结果 |
|--------|----------|----------|
| 上下文晋升 | D1→D2→D7 | 运营晋升的促销规则，数据分析师可见 |
| 私有隔离 | D3→D4 vs D5→D6 | 谈判底价和 A/B 测试结果各自私有，互不可见 |
| 双向协作 | D8→D9（→D10） | 共享空间同时包含促销规则 + 推送时间建议 |
| 晋升选择性 | D3 vs D9 | 未晋升的敏感信息不出现在共享空间 |

---

#### Phase E：企业治理全景（9 步，Phase 2 新增能力）

> **Phase E 演示的所有能力均来自 Phase 2**——显式 ACL、关键字遮蔽、
> 分层审计、免复制跨团队共享。Phase D 证明了 Phase 1 的协作与隔离，
> Phase E 在其基础上展示治理层的升级。
>
> **双视角演示**：管理员在 curl 终端操作策略，数据分析师在 TUI 中
> 实时"体验"效果。两个窗口同时可见，形成直观的对比。

##### 故事延续

Phase D 结束后，运营负责人（query-agent）意识到：

- 供应商谈判底价虽然在私有空间，但万一将来被误操作 promote，
  敏感信息就会泄漏。**需要一道额外的保险**。
- 已经共享到团队的促销规则里包含具体折扣比例（60%），对外部
  协作者来说知道规则就够了，**不需要看到具体数字**。
- 新来了一位实习生 agent，只需要读 API 规范文档，**不应该加入
  整个工程部团队**。
- 作为管理员，需要**确认所有操作都有迹可循**。

##### 前置准备

1. **先完成 Phase D**（或至少跑过 `demo_phase2.py` 确保 query-agent
   有 admin 角色和供应商成本文档存在）
2. 保持完整 5 终端栈运行
3. Sidecar 以 `--agent-id analysis-agent` 运行在 :9100（Phase D 结束
   后应该已经是 analysis-agent）
4. 额外开一个终端作为 **admin curl 终端**（Terminal 6），用于管理员操作

##### 终端布局

录屏时保持以下终端可见（建议左右分屏）：

| 终端 | 内容 | 角色 |
|------|------|------|
| Terminal 5 | OpenClaw TUI | 数据分析师（analysis-agent）—— 主画面 |
| Terminal 6 | curl 命令 | 管理员（query-agent）—— 策略操作 |
| Terminal 2 | ContextHub Server | 请求日志（观察 403 等） |
| Terminal 3 | Python Sidecar | dispatch/assemble 日志 |

##### 角色与验证能力映射

| 系统标识 | 业务角色 | 操作方式 |
|----------|----------|----------|
| query-agent | 运营负责人 / 管理员 | Terminal 6（curl） |
| analysis-agent | 数据分析师 | Terminal 5（TUI） |

| 验证能力 | 含义 | 对应步骤 | 来源 |
|----------|------|----------|------|
| 显式 ACL 拦截 | deny policy → 读被阻止 | E1-E3 | Phase 2 |
| 关键字遮蔽 | field_masks → `[MASKED]` | E4-E5 | Phase 2 |
| 免复制共享 | share grant → 同 URI 可读 | E6-E7 | Phase 2 |
| 审计合规 | 操作记录可查 | E8-E9 | Phase 2 |

##### Part 1：ACL Deny — 显式访问拦截

**Step E1 — 【TUI 基线】数据分析师确认当前可以读取供应商成本**

在 TUI（Terminal 5）中输入：

```
请读取 ctx://team/engineering/docs/supplier-costs 的内容
```

预期：agent 调用 `read`，成功返回供应商成本明细内容。

> **这是设置 deny 之前的基线**。观众先看到"能读到"，后面看到
> "读不到了"，对比效果更强。

**Step E2 — 【curl 管理操作】管理员设置 deny policy**

在 Terminal 6 中执行：

```bash
curl -X POST http://localhost:8000/api/v1/admin/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: changeme" \
  -H "X-Account-Id: acme" \
  -H "X-Agent-Id: query-agent" \
  -d '{
    "resource_uri_pattern": "ctx://team/engineering/docs/supplier-costs",
    "principal": "agent:analysis-agent",
    "effect": "deny",
    "actions": ["read"],
    "priority": 10
  }'
```

预期：返回 201 + policy 对象。记录返回的 `id`（后续删除需要）。

> **旁白提示**：可以向观众说明——"管理员发现供应商成本文档不应该
> 对数据分析师可见，现在通过 Admin API 设置一条 deny 策略。"

**Step E3 — 【TUI 体验】数据分析师再次尝试读取 → 被拦截**

在 TUI（Terminal 5）中输入：

```
请再读一次 ctx://team/engineering/docs/supplier-costs
```

预期：
- agent 调用 `read`，但这次**失败**
- Terminal 2 Server 日志出现 **403**
- TUI 中 agent 会告知用户"无法读取"或"权限不足"

> **这是 Phase E 最直观的一幕**：同一个 agent、同一条命令，
> 管理员在另一个终端设了一条 deny 策略后，立刻就读不到了。
> Phase D 的隔离靠"不共享"（被动），Phase E 靠"显式禁止"（主动）。

##### Part 2：关键字遮蔽 — 看得到结构，看不到敏感数字

**Step E4 — 【curl 管理操作】删除 deny + 创建遮蔽策略**

在 Terminal 6 中执行：

```bash
# 先删除 Step E2 的 deny policy（用 E2 返回的 policy id）
curl -X DELETE http://localhost:8000/api/v1/admin/policies/<E2_POLICY_ID> \
  -H "X-API-Key: changeme" \
  -H "X-Account-Id: acme" \
  -H "X-Agent-Id: query-agent"

# 创建含 field_masks 的 allow 策略
curl -X POST http://localhost:8000/api/v1/admin/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: changeme" \
  -H "X-Account-Id: acme" \
  -H "X-Agent-Id: query-agent" \
  -d '{
    "resource_uri_pattern": "ctx://team/engineering/docs/supplier-costs",
    "principal": "agent:analysis-agent",
    "effect": "allow",
    "actions": ["read"],
    "field_masks": ["60%", "55%", "58%", "50%", "底价", "底线"],
    "priority": 5
  }'
```

预期：deny 删除返回 204，遮蔽策略创建返回 201。

> **旁白提示**："管理员换了一种策略——不再完全禁止，而是允许
> 读取但遮蔽具体数字。分析师能看到文档结构，但看不到折扣比例。"

**Step E5 — 【TUI 体验】数据分析师读到遮蔽后的内容**

在 TUI（Terminal 5）中输入：

```
请读取 ctx://team/engineering/docs/supplier-costs 的内容
```

预期：
- agent 调用 `read`，这次**成功**
- 但返回的内容中，"60%"、"55%"、"58%"、"50%"、"底价"、"底线"
  等关键词全部显示为 **`[MASKED]`**
- Terminal 3 sidecar 日志可以看到实际返回的 masked 内容

> **关键证据**：观众可以看到 agent 确实读到了文档（不是 403），
> 但所有敏感数字和关键词都被替换成了 `[MASKED]`。
> 这就是 Phase 2 的关键字遮蔽能力。

##### Part 3：Share Grant — 免复制跨团队共享

**Step E6 — 【curl 管理操作】授权数据分析师读取 API 规范文档**

在 Terminal 6 中执行：

```bash
curl -X POST http://localhost:8000/api/v1/shares \
  -H "Content-Type: application/json" \
  -H "X-API-Key: changeme" \
  -H "X-Account-Id: acme" \
  -H "X-Agent-Id: query-agent" \
  -d '{
    "source_uri": "ctx://team/engineering/docs/api-standards",
    "target_principal": "agent:analysis-agent"
  }'
```

预期：返回 201 + policy 对象（`conditions: {"kind": "share_grant"}`）。

> **旁白提示**："管理员不想把分析师加入整个工程部，只想让他
> 看到这一份 API 规范。通过 share grant，直接授权这个 URI
> 给特定 agent，不需要复制文档，也不需要改变团队归属。"

**Step E7 — 【TUI 体验】数据分析师读取共享的 API 规范**

在 TUI（Terminal 5）中输入：

```
请读取 ctx://team/engineering/docs/api-standards 的内容
```

预期：
- agent 调用 `read`，成功返回 API 规范内容
- 这是一个 analysis-agent 默认不可见的资源，通过 share grant 变得可读

> **对比 Phase D 的 promote**：Phase D 中 promote 是把内容复制到
> 团队共享路径（所有团队成员可见）；Phase E 的 share grant 是
> 直接在原 URI 上授权给一个人（精细控制），不产生副本。

##### Part 4：审计日志 — 合规证据

**Step E8 — 【TUI 体验】数据分析师随意提问，产生更多审计记录**

在 TUI（Terminal 5）中输入：

```
项目组目前共享了哪些知识？请列出 ctx://team/engineering/memories/shared_knowledge
```

预期：agent 调用 `ls`，返回 Phase D 中晋升的共享记忆列表。
这条操作会作为一条 `ls` 审计记录被记录下来。

**Step E9 — 【curl 管理操作】查看审计日志**

在 Terminal 6 中执行：

```bash
curl "http://localhost:8000/api/v1/admin/audit?limit=20" \
  -H "X-API-Key: changeme" \
  -H "X-Account-Id: acme" \
  -H "X-Agent-Id: query-agent" | python3 -m json.tool
```

预期：返回审计记录列表，包含：

| action | result | 说明 | 来自 |
|--------|--------|------|------|
| `policy_change` | `success` | 创建 deny policy | E2 |
| `access_denied` | `denied` | analysis-agent 被拦截 | E3 |
| `policy_change` | `success` | 删除 deny + 创建遮蔽 policy | E4 |
| `read` | `success` | 读取遮蔽后的内容 | E5 |
| `policy_change` | `success` | 创建 share grant | E6 |
| `read` | `success` | 读取共享的 API 规范 | E7 |
| `ls` | `success` | 列出共享空间 | E8 |

> **审计日志是 Phase 2 独有能力**。Phase 1 的所有操作（D1-D10）
> 不会产生审计记录；Phase 2 之后，所有策略变更、访问拒绝、读操作
> 都被记录在 `audit_log` 表中。
>
> **向观众展示**：可以 `| python3 -m json.tool` 格式化输出，
> 或选取 1-2 条有代表性的记录放大讲解（如 `access_denied` 那条，
> 对应 Step E3 中 TUI 读取失败的那一刻）。

##### Phase E 验证要点总结

| 验证点 | 管理操作（curl） | 体验验证（TUI） | Phase 2 能力 |
|--------|-----------------|-----------------|-------------|
| ACL 拦截 | E2 创建 deny | E1 基线可读 → E3 被拒 | ACL 读覆盖层 |
| 关键字遮蔽 | E4 创建 masking 策略 | E5 敏感词 → `[MASKED]` | MaskingService |
| 免复制共享 | E6 创建 share grant | E7 原本不可见 → 可读 | ShareService |
| 审计合规 | E9 查询 audit_log | E8 产生审计记录 | AuditService |

##### Phase D + E 组合录屏建议

| 录屏段落 | 内容 | 操作方式 | 时长估计 | 覆盖能力 |
|----------|------|----------|----------|----------|
| Phase D（10 步） | 协作全景：存储、晋升、隔离、双向共享 | 全 TUI | 5-8 min | Phase 1 |
| Phase E（9 步） | 治理全景：ACL deny、遮蔽、share、审计 | curl + TUI 双视角 | 5-8 min | Phase 2 |
| 合计 | 完整 MVP 能力展示 | | 10-16 min | Phase 1 + 2 |

> **录屏窗口布局建议**：左侧放 TUI（大窗口，主画面），右侧上方放
> curl 终端（管理员操作），右侧下方放 Server 日志（观察 403 等）。
> 这样观众同时看到"管理员做了什么"和"agent 的反应"，对比效果最强。
>
> Phase E 的 TUI 步骤受模型 tool-use 决策影响。如果 agent 没有主动
> 调用 `read` 工具，可以更直接地说：
> "请调用 read 工具，URI 是 ctx://team/engineering/docs/supplier-costs"。
> curl 步骤是确定性的，TUI 步骤是演示性的——两者互补。

