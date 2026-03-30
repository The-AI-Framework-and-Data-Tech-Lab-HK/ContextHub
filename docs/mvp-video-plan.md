#### DEMO：多 Agent 协作全景（10 步）

> 本 demo 是独立的完整演示流程，一次性覆盖三个核心能力：
>
> | 能力 | 含义 | 对应步骤 |
> |------|------|----------|
> | 跨 Agent 上下文晋升 | agent A promote → agent B 可见 | D1-D2, D7 |
> | 私有空间隔离 | 各 agent 的私有记忆互不可见 | D3-D4, D5-D6 |
> | 双向协作共享 | 两个 agent 都向共享空间贡献知识 | D8-D9, D10 |


**演示场景**：公司春季营销活动的跨部门协作。

| 系统标识 | 业务角色 | 职责 |
|----------|----------|------|
| query-agent | 运营负责人 | 策划活动规则、对接供应商 |
| analysis-agent | 数据分析师 | 分析用户行为、提供数据洞察 |
| engineering 团队 | 项目组共享空间 | 两人协作的公共知识库 |

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

观察 Terminal 3 sidecar 日志出现 `dispatch contexthub_store` 调用。

**Step D2 — 晋升到团队共享空间**

在 TUI 中输入：

```
请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
```

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

观察 sidecar 日志出现 `dispatch contexthub_store`，但**不会**出现
`contexthub_promote`。

**Step D4 — 验证运营负责人的私有空间**

在 TUI 中输入：

```
请列出我的私有空间的所有记忆
```

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

观察 sidecar 日志出现 `dispatch contexthub_store`。

**Step D6 — 验证隔离：数据分析师的私有空间不包含运营负责人的记忆**

在 TUI 中输入：

```
请列出我的私有空间的所有记忆
```

预期：
- **只包含** Step D5 刚存的 A/B 测试初步结果
- **不包含** 运营负责人的"供应商谈判底价"记忆（Step D3）

> **这是私有隔离的关键证据**：运营负责人在 Step D4 中看到两条私有
> 记忆，而数据分析师只能看到自己的那一条。两个 agent 的私有空间
> 完全独立，互不干扰 —— 敏感的谈判底价不会泄漏给其他角色。

**Step D7 — 验证共享：数据分析师能看到运营负责人晋升的活动规则**

在 TUI 中输入：

```
请列出 ctx://team/engineering/shared_knowledge 下的内容
```

预期：列表中包含运营负责人在 Step D2 晋升的春季促销规则 —— 这
证明跨 Agent 的上下文晋升在 runtime 中生效。

**Step D8 — 数据分析师也向共享空间贡献自己的洞察**

在 TUI 中输入：

```
请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00
是下单高峰期，建议将促销推送时间安排在 19:30。
然后把这条晋升到团队共享空间 engineering。
```

观察 sidecar 日志依次出现 `dispatch contexthub_store` 和
`dispatch contexthub_promote`。

> **提示**：如果 agent 没有一次性完成存储和晋升，可以分两步引导，
> 或直接指定 URI："请调用 contexthub_promote，把 URI
> ctx://agent/analysis-agent/memories/xxx 晋升到 engineering"。

**Step D9 — 验证协作成果：共享空间包含两个角色的贡献**

在 TUI 中输入：

```
请列出 ctx://team/engineering/shared_knowledge 下的内容
```

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
请列出 ctx://team/engineering/shared_knowledge 下的内容
```

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

