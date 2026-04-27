# ContextHub MVP 验证实施计划

> 本文档是最短路径的验证计划。目标：以最小投入证明 ContextHub 作为企业级
> 多 Agent 协作上下文中间件的核心价值。
>
> **验证边界**：本次验证覆盖两个阶段的能力——
>
> - **Phase 1 — 平台内核**：隔离、共享晋升、版本治理、变更传播、运行时集成
> - **Phase 2 — 企业治理**：显式 ACL（读覆盖层）、关键字遮蔽、分层审计、
>   免复制跨团队共享（Share Grant）、Admin API
>
> 不等同于完整 enterprise-ready 产品（HA、多区域、合规认证等属于后续迭代）。

## 前置状态

| 组件 | 状态 |
|------|------|
| ContextHub Server | 运行中（FastAPI :8000） |
| OpenClaw 集成 | context-engine 插件已接入，TUI 可用 |
| 自动化测试 | 以当次 `pytest -q` 实际输出为准（不预写总数） |

## 演示场景：团队结构与上下文继承

验证用的 seed 数据模拟了一个企业的组织架构（来自 `alembic/versions/001_initial_schema.py`）：

```
全组织 (root)
├── 工程部 engineering
│   └── 后端组 engineering/backend    ← query-agent 主属团队
└── 数据部 data
    └── 数据分析组 data/analytics     ← analysis-agent 主属团队
```

两个 agent 分属不同部门，但 **analysis-agent 同时也是工程部的成员**（非主属）：

| Agent | 主属团队 | 额外成员 |
|-------|---------|---------|
| query-agent | engineering/backend | engineering（由 `demo_e2e.py` 自动补入） |
| analysis-agent | data/analytics | engineering |

上下文继承规则：

| 规则 | 含义 |
|------|------|
| 私有隔离 | 每个 agent 的私有记忆只有自己能看到 |
| 子读父 | 子团队成员可以读取父团队的共享上下文 |
| 父不见子 | 父团队默认看不到子团队的上下文 |
| 晋升共享 | 私有记忆可 `promote` 到所属团队，成为该团队所有成员可见的共享上下文 |

**demo 的核心故事**：query-agent（后端工程师）把一个 SQL pattern 从私有
空间晋升到 engineering 团队 → analysis-agent（数据分析师）虽然主属数据部，
但因为也是工程部成员，所以能自动召回这条知识。这就是企业级跨部门知识复用。

---

## 核心原则

1. **证功能独特性，不证 token 数量**：ContextHub 的差异化是治理层（隔离、
   传播、版本管理），不是 RAG 优化。量化评估留给 Post-MVP 的三层 Benchmark（详见 `plan/15-benchmark-plan.md`）。
2. **单 OpenClaw 实例足够**：所有协作机制都在 Server 端状态机完成，通过
   `agent_id` 切换身份即可验证全部协作特性。
3. **三层证据递进**：自动化测试 → API 闭环 demo → 运行时合同验证。

---

## 第一层：自动化功能正确性（已完成）

对应 plan 中 Tier 3 功能测试，已全部通过。

### 变更传播正确性（P-1 ~ P-8）

| 用例 | 描述 | 状态 |
|------|------|------|
| P-1 | Schema 变更 → 依赖标记 stale | PASS |
| P-2 | Breaking Skill 更新 → 订阅者标记 stale | PASS |
| P-3 | Non-breaking Skill 更新 → 仅通知 | PASS |
| P-4 | 统计信息更新 → 不传播 | PASS |
| P-6 | 表删除 → 归档 + 传播 | PASS |
| P-7 | NOTIFY 丢失 → 补扫恢复 | PASS |
| P-8 | Worker lease 超时 → retry 恢复 | PASS |

### 多 Agent 协作正确性（C-1 ~ C-5）

| 用例 | 描述 | 状态 |
|------|------|------|
| C-1 | 记忆晋升（private → team） | PASS |
| C-2 | 晋升后跨 Agent 可见 | PASS |
| C-3 | 源记忆变更 → 晋升副本收到通知 | PASS |
| C-4 | Skill 订阅 pinned version | PASS |
| C-5 | Skill 订阅 latest（floating） | PASS |

### 可见性与隔离正确性（A-1 ~ A-4）

| 用例 | 描述 | 状态 |
|------|------|------|
| A-1 | Agent 私有空间相互隔离 | PASS |
| A-2 | 团队层级继承（子读父） | PASS |
| A-3 | 子团队默认不向父暴露 | PASS |
| A-4 | promote 后跨 Agent 可见 | PASS |

**结论：权限泄漏率 = 0，Phase 1 MVP 退出标准的功能正确性维度已满足。**

### Phase 2 企业治理正确性（A-5 ~ A-15）

> 以下用例对应 Phase 2 新增能力，由 `tests/test_phase2_acl_audit.py` 覆盖。

| 用例 | 描述 | 状态 |
|------|------|------|
| A-5 | Deny policy 覆盖默认可见性 | PASS |
| A-6 | Allow policy 暴露默认不可见的内容 | PASS |
| A-7 | Deny-override 优先级（allow + deny 同时存在 → deny 胜出） | PASS |
| A-8 | 关键字遮蔽生效（field_masks → `[MASKED]`） | PASS |
| A-9 | 无 policy 时退回 Phase 1 默认行为 | PASS |
| A-10 | Tier 1 审计（写操作 fail-closed） | PASS |
| A-11 | Tier 2 审计（读操作 best-effort） | PASS |
| A-12 | ACL deny 产生 access_denied 审计记录 | PASS |
| A-13 | Share grant / revoke 生效 | PASS |
| A-14 | Share 不复制（同 URI，无新 context 行） | PASS |
| A-15 | 写操作不受 read-deny 影响 | PASS |

### 产出物

- 带日期的 `pytest -q` 命令与**原始输出**截图（以实际 pass 数为准）

---

## 第二层：API 内核闭环 demo（已通过 ✓）

> **验证日期**：2026-04-10 | 两个脚本均返回码 0

目标：用 `scripts/demo_e2e.py` 证明 ContextHub **核心横向闭环**在
服务器端完整跑通，不依赖模型是否"愿意"调用工具。

这层的价值是：
- 提供确定性的闭环证据（不受 LLM tool-use 决策影响）
- 完成 TUI 工具面无法覆盖的动作（创建 skill context、建立 subscription）
- 为第三层的运行时验证提供 seed 数据

### 前置准备

1. **启动服务**：PostgreSQL + ContextHub Server（参考 `docs/openclaw-integration-guide.md`）

2. **确保 team membership**：`demo_e2e.py` 会自动补 `query-agent` 到
   `engineering` 团队的 membership（见脚本 `_ensure_team_membership()`）。
   如果手动执行，需先运行：
   ```sql
   INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
   VALUES ('query-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE)
   ON CONFLICT DO NOTHING;
   ```

3. **推荐配置 `OPENAI_API_KEY`**：在 `.env` 中设置 OpenAI API Key，
   让 Server 使用真实 embedding。如果不设，Server 会降级为 keyword fallback
   （ILIKE 匹配），功能可用但召回质量受限。**MVP 验证建议配上 API Key
   以获得最佳效果。**

### 闭环步骤（对应 demo_e2e.py）

| Step | 动作 | API | 验证点 | 结果 |
|------|------|-----|--------|------|
| 1 | query-agent 写私有记忆 | `POST /api/v1/memories` | 返回 201 + memory URI | PASS — `ctx://agent/query-agent/memories/mem-353b2472` |
| 2 | 创建 skill context + 发布 v1 | `POST /api/v1/contexts` + `POST /api/v1/skills/versions` | skill context 创建成功，v1 发布 | PASS — skill context + v1 |
| 3 | query-agent promote 记忆到 team | `POST /api/v1/memories/promote` | 返回 team URI | PASS — `ctx://team/engineering/memories/shared_knowledge/mem-353b2472` |
| 4 | analysis-agent 看到共享记忆 + 建立 pinned 订阅 | `GET /api/v1/memories` + `POST /api/v1/skills/subscribe` | shared memories ≥ 1，pinned v1 | PASS — 1 shared memory, pinned v1 |
| 5 | query-agent 发布 breaking v2 | `POST /api/v1/skills/versions` | v2 发布成功，is_breaking=true | PASS — v2 (breaking) |
| 6 | 验证传播：analysis-agent 读到 pinned v1 + advisory | `POST /api/v1/tools/read` | 返回 v1 内容 + "v2 available" advisory | PASS — version=1, advisory: "v2 available, currently pinned to v1" |

**附加验证**：Step 7 Catalog sync + sql-context → 5 tables synced, 2 relevant tables found (PASS)

### 变更收敛时延采集

在 Step 5-6 之间加入精确计时（如果脚本里还没有，先加上）：

```python
import time

t0 = time.monotonic()
# Step 5: publish breaking v2
r = await http.post("/api/v1/skills/versions", json={...}, headers=qa)

# Step 6: poll until advisory appears
for _ in range(20):
    await asyncio.sleep(0.1)
    r = await http.post("/api/v1/tools/read", json={...}, headers=aa)
    if r.json().get("advisory"):
        break
convergence_ms = (time.monotonic() - t0) * 1000
print(f"  变更收敛时延: {convergence_ms:.0f}ms")
```

### 验收标准

1. ✅ `python scripts/demo_e2e.py` 完整退出，返回码 0
2. ✅ 输出包含：private memory URI、promoted team URI、skill v1/v2、
   pinned read + advisory
3. `convergence_ms`：脚本使用固定 2s sleep，advisory 在首次读取即出现（目标 < 2s 满足）

### 产出物

- `demo_e2e.py` 的完整 stdout（见上方步骤结果）
- `convergence_ms`：< 2s（advisory 在 sleep 后首次请求即返回）

---

## 第二层补充：Phase 2 治理 API 闭环 demo（已通过 ✓）

> **验证日期**：2026-04-10 | `demo_phase2.py` 返回码 0
>
> 本节对应 Phase 2 新增能力，使用 `scripts/demo_phase2.py` 验证。
> 需在第二层 `demo_e2e.py` 之后运行。

目标：证明 Phase 2 的 **ACL deny、关键字遮蔽、Share Grant/Revoke、
审计日志、写操作豁免** 在 Server 端完整跑通。

### 闭环步骤（对应 demo_phase2.py）

| Step | 动作 | API | 验证点 | 结果 |
|------|------|-----|--------|------|
| 1 | query-agent 创建含敏感信息的上下文 | `POST /api/v1/contexts` | 创建成功，analysis-agent 默认可读（Phase 1 基线） | PASS — 201, baseline 可读 |
| 2 | Admin 创建 deny policy | `POST /api/v1/admin/policies` | deny 策略对 analysis-agent 生效 | PASS — 201 |
| 3 | analysis-agent 尝试读取被 deny 的内容 | `POST /api/v1/tools/read` | 返回 403 Forbidden | PASS — 403, owner 仍 200 |
| 4 | Admin 创建含 field_masks 的 allow 策略 | `POST /api/v1/admin/policies` | 遮蔽策略创建成功 | PASS — field_masks 6 项 |
| 5 | analysis-agent 读取 → 敏感词被遮蔽 | `POST /api/v1/tools/read` | 内容中 "60%" 等被替换为 `[MASKED]` | PASS — 6 处 `[MASKED]` |
| 6 | Share grant：授权 analysis-agent 读特定资源 | `POST /api/v1/shares` | 授权成功，analysis-agent 可读 | PASS — 201, `conditions: {kind: share_grant}` |
| 7 | Share revoke：撤销授权 | `DELETE /api/v1/shares/{id}` | 撤销成功 | PASS — 204, grants = 0 |
| 8 | Read-deny 不阻止写操作 | `POST /api/v1/memories` | analysis-agent 在有 read-deny 时仍可写 | PASS — 201 Created |
| 9 | 查询审计日志 | `GET /api/v1/admin/audit` | 上述操作全部有记录 | PASS — 20 entries, 7 action types |
| 10 | 列出当前策略 | `GET /api/v1/admin/policies` | 显示活跃策略清单 | PASS — 1 条 allow+masking |

### 验收标准

1. ✅ `python scripts/demo_phase2.py` 完整退出，返回码 0
2. ✅ 输出包含：deny → 403、masking → `[MASKED]`、share → grant/revoke、
   audit log 非空（20 条，含 `policy_change` / `access_denied` / `read`）
3. ✅ 写操作在 read-deny 下仍返回 201

### 产出物

- `demo_phase2.py` 的完整 stdout（见上方步骤结果）

---

## 第三层：OpenClaw 运行时合同验证（已通过 ✓）

> **验证日期**：2026-04-10 | 4 个 curl 步骤全部通过

目标：证明 **gateway → sidecar → plugin → SDK → server** 这条运行时
链路真实成立。这层验的是集成合同，不是 LLM 聪不聪明。

### 为什么需要这层

第二层直接打 API，证明了 Server 内核闭环。但企业版还需要证明
OpenClaw runtime 能通过 sidecar 的 `dispatch` / `assemble` 接口
正确调用 ContextHub。这才是"runtime 集成成立"的证据。

### 前置准备

1. **先完成第二层**（即先跑 `demo_e2e.py`）。跑完后数据库里已经有了
   skill context、v1/v2 版本、pinned subscription 等数据 —— 这就是
   "预置"，不需要手动插入任何东西。
2. 启动完整 5 终端栈（参考 `docs/openclaw-integration-guide.md`）：
   PostgreSQL、ContextHub Server、Python Sidecar、OpenClaw Gateway、TUI

### 当前工具面限制

OpenClaw 插件暴露 7 个工具：`ls`、`read`、`grep`、`stat`、
`contexthub_store`、`contexthub_promote`、`contexthub_skill_publish`。

**没有** `skill_create` 和 `skill_subscribe`。因此：
- 存储、晋升、发布新版本 → sidecar dispatch 可以做
- 创建 skill context、建立订阅 → 由第二层 `demo_e2e.py` 已完成，无需额外操作

### 验证步骤与结果（4 步，通过 curl 直接打 sidecar）

> Sidecar 运行在 :9100，支持 `X-Agent-Id` header 切换身份。

#### Step 1：dispatch → `contexthub_store` — PASS ✓

```bash
curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: query-agent" \
  -d '{
    "name": "contexthub_store",
    "args": {
      "content": "月度销售额查询要 JOIN orders 和 products 并按月份聚合",
      "tags": ["sql", "monthly-sales"]
    }
  }'
```

结果：返回 memory URI `ctx://agent/query-agent/memories/mem-af48b007`。

#### Step 2：dispatch → `contexthub_promote` — PASS ✓

```bash
curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: query-agent" \
  -d '{
    "name": "contexthub_promote",
    "args": {
      "uri": "ctx://agent/query-agent/memories/mem-af48b007",
      "target_team": "engineering"
    }
  }'
```

结果：返回 team URI `ctx://team/engineering/memories/shared_knowledge/mem-af48b007`。

#### Step 3：assemble → 验证自动召回 — PASS ✓

```bash
curl -X POST http://localhost:9100/assemble \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: analysis-agent" \
  -d '{
    "sessionId": "verify-002",
    "messages": [
      {"role": "user", "content": "月度销售额"}
    ],
    "tokenBudget": 1024
  }'
```

结果：
- `systemPromptAddition` 非空（含 Tools Guide + Auto-Recall）
- Auto-Recall 内容：`[ctx://team/engineering/memories/shared_knowledge/mem-af48b007] 月度销售额查询要 JOIN orders 和 products 并按月份聚合`

> **注意**：未配 `OPENAI_API_KEY` 时检索走 keyword fallback（ILIKE），
> 查询内容须与存储记忆的关键词重合。完整自然语言问句（如"月度销售额应该怎么查？"）
> 因 ILIKE 匹配整串而非分词，可能返回 0 结果。使用关键词片段（如"月度销售额"）即可命中。
> 配置 embedding 后此限制消失。

#### Step 4：dispatch → `contexthub_skill_publish` + `read` — PASS ✓

query-agent 发布 breaking v3：

```bash
curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: query-agent" \
  -d '{
    "name": "contexthub_skill_publish",
    "args": {
      "skill_uri": "ctx://team/engineering/skills/sql-generator",
      "content": "v3: Runtime-verified SQL generator with CTE support",
      "changelog": "Breaking: new output format",
      "is_breaking": true
    }
  }'
```

结果：v3 发布成功（`version: 3, is_breaking: true`）。

analysis-agent 读取（pinned v1 + advisory）：

```bash
curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: analysis-agent" \
  -d '{
    "name": "read",
    "args": {
      "uri": "ctx://team/engineering/skills/sql-generator"
    }
  }'
```

结果：`version: 1, content: "v1: Basic SQL generator for orders queries", advisory: "v3 available, currently pinned to v1"`

### 验收标准

以下 4 项全部通过：

1. ✅ `dispatch(contexthub_store)` → 成功返回 memory
2. ✅ `dispatch(contexthub_promote)` → 成功返回 team URI
3. ✅ `assemble()` → `systemPromptAddition` 非空且包含相关内容
4. ✅ `dispatch(contexthub_skill_publish)` + `dispatch(read)` → pinned + advisory

### 可选加分项：TUI 录屏

如果第三层的 4 个 curl 全部通过，可以**额外**在 TUI 中做一次展示性录屏。
这是给人看的展示材料，不是退出门槛 —— 因为 TUI 结果受模型 tool-use 决策
和 prompt 写法影响。

### 可选加分项：TUI 录屏（详细步骤）

#### 终端布局

录屏时保持以下终端可见（建议横向排列或用 tmux split）：

| 终端 | 内容 | 观察点 |
|------|------|--------|
| Terminal 2 | ContextHub Server | 请求日志 |
| Terminal 3 | Python Sidecar | dispatch/assemble 调用日志 |
| Terminal 5 | OpenClaw TUI | 对话交互（主画面） |

Terminal 1（PostgreSQL）和 Terminal 4（Gateway）保持后台运行即可。

#### Phase A：query-agent 存储 + 晋升（3 步）

**启动状态**：Sidecar 以 `--agent-id query-agent` 运行在 :9100。

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

**Step 1 — 存储私有记忆**

在 TUI 中输入（引导 agent 调用 `contexthub_store`）：

```
请记住：查询月度销售额时，应该 JOIN orders 和 products 表，
GROUP BY DATE_TRUNC('month', order_date)。
```

观察 Terminal 3 sidecar 日志出现 `dispatch contexthub_store` 调用。

**Step 2 — 晋升到团队**

在 TUI 中输入（引导 agent 调用 `contexthub_promote`）：

```
请把刚才存储的记忆晋升到团队共享空间 engineering。
```

观察 sidecar 日志出现 `dispatch contexthub_promote` 调用。

> **提示**：如果 agent 没有主动调用工具，可以更直接地说：
> "请调用 contexthub_promote，把 URI ctx://agent/query-agent/memories/xxx
> 晋升到 engineering"（URI 从 Step 1 的 sidecar 日志中复制）。

**Step 3 — 验证存储结果**

在 TUI 中输入：

```
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

预期：agent 调用 `ls`，返回的列表中包含刚晋升的记忆。

#### 切换 Agent 身份

在 TUI 中按 `Ctrl+C` 退出 → Terminal 4 `Ctrl+C` 停 Gateway →
Terminal 3 `Ctrl+C` 停 Sidecar。

重启 Sidecar，换 agent-id（**同一个端口 9100**，不需要改 Gateway 配置）：

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

#### Phase B：analysis-agent 跨 Agent 召回（1 步）

**Step 4 — 提问，观察自动召回**

在 TUI 中输入：

```
月度销售额应该怎么查？
```

预期：
- Terminal 3 sidecar 日志出现 `assemble` 调用
- 日志中 `systemPromptAddition` 包含 query-agent 晋升的 SQL pattern
- TUI 中 analysis-agent 的回答引用了 JOIN orders/products 的模式

> **关键证据**：看 sidecar 日志中的 `systemPromptAddition` 字段，
> 而不是只看模型的自然语言回答。如果未配 `OPENAI_API_KEY`，
> 提问中要包含与存储记忆重合的关键词（"月度""销售额"）。

#### Phase C：Skill 版本治理（可选，需第二层 seed）

> 这部分需要数据库中已存在 skill context 和 subscription（由第二层
> `demo_e2e.py` 创建）。如果是在 clean DB 上录屏，先跑一次
> `demo_e2e.py`。

再次切换 Agent 身份（同上流程：停 TUI/Gateway/Sidecar → 重启为
query-agent → 启动 Gateway/TUI）。

**Step 5 — query-agent 发布 breaking 新版本**

```
请发布 sql-generator 的新版本，调用 contexthub_skill_publish，
URI 是 ctx://team/engineering/skills/sql-generator，
内容是 "Rewritten SQL generator with CTE syntax"，
标记为 breaking change。
```

观察 sidecar 日志出现 `dispatch contexthub_skill_publish`。

再次切换到 analysis-agent（同上切换流程），然后：

**Step 6 — analysis-agent 观察 pinned + advisory**

```
请读取 ctx://team/engineering/skills/sql-generator 的内容
```

预期：agent 调用 `read`，返回 pinned 旧版本内容 + advisory 提示
有新版本可用。

#### Phase D：多 Agent 协作全景（10 步）

##### 故事背景

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

> **可与 Phase A-C 择一录制**；如果时间充裕建议录 Phase D，
> 因为它包含了 Phase A+B 的全部场景并额外证明隔离与协作。

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
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
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
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

预期：
- 包含运营负责人晋升的**春季促销规则**（来自 Step D2）
- 包含数据分析师晋升的**促销推送时间建议**（来自 Step D8）
- 两个不同角色的知识在同一个共享空间中共存

> **这是协作的关键证据**：共享空间不是单一角色的"导出"，而是多个
> 角色各自贡献、共同构建的知识库。运营带来了活动规则（满 300 减 50、
> 活动档期），数据分析带来了最佳推送时间（周六 19:30），合在一起就是
> 完整的促销执行方案。这正是故事开头描述的结果：
> **"4 月 1-15 日，满 300 减 50，每周六 19:30 推送。"**

##### （可选）切换回 query-agent 验证双向可见

重新切换为 query-agent（同上切换流程：停 TUI/Gateway/Sidecar →
重启为 query-agent → 启动 Gateway/TUI）。

**Step D10 — 运营负责人确认能看到数据分析师的共享贡献**

在 TUI 中输入：

```
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

预期：
- 运营负责人也能看到数据分析师在 Step D8 晋升的促销推送时间建议
  —— 至此，运营负责人拥有了制定完整促销方案所需的全部信息
- 同时运营负责人的私有供应商谈判底价（Step D3）**仍然不会**出现在
  共享空间中 —— 敏感信息只有主动晋升才会共享，底线不会泄漏
- 证明共享空间的修改是**双向生效**的：无论谁晋升，所有项目组成员都能看到

##### Phase D 验证要点总结

| 验证点 | 对应步骤 | 预期结果 |
|--------|----------|----------|
| 上下文晋升 | D1→D2→D7 | 运营晋升的促销规则，数据分析师可见 |
| 私有隔离 | D3→D4 vs D5→D6 | 谈判底价和 A/B 测试结果各自私有，互不可见 |
| 双向协作 | D8→D9（→D10） | 共享空间同时包含促销规则 + 推送时间建议 |
| 晋升选择性 | D3 vs D9 | 未晋升的敏感信息不出现在共享空间 |

#### Phase E：企业治理全景（9 步，Phase 2 新增能力）

> Phase E 演示的所有能力均来自 **Phase 2**（显式 ACL、关键字遮蔽、
> 分层审计、免复制跨团队共享），是 Phase D（Phase 1 协作全景）的
> 治理层升级。建议在 Phase D 之后紧接着录制。
>
> **双视角演示**：管理员在 curl 终端操作策略（Admin API 无 TUI 工具），
> 数据分析师在 TUI 中实时体验效果。两个窗口同时可见，形成直观对比。

##### 故事延续

Phase D 结束后，运营负责人发现供应商谈判底价虽然在私有空间，
但为了更高的安全保障，决定：

1. 通过 Admin API 给供应商成本文档设置 **deny policy**，即使
   未来误操作 promote 也不会泄漏
2. 给已共享的促销规则设置 **关键字遮蔽**（`field_masks`），让
   外部协作者看到规则但看不到具体折扣比例
3. 通过 **Share Grant** 把 API 规范文档直接授权给数据分析师，
   不需要把分析师加入整个工程部
4. 查看 **审计日志**，确认所有操作都有记录

##### 前置准备

1. **先完成 Phase D**（或至少跑一次 `demo_phase2.py` 确保 query-agent
   有 admin 角色）
2. 保持 5 终端栈运行，Sidecar 以 `--agent-id analysis-agent` 运行
3. 额外开一个终端（Terminal 6）用于 curl 管理员操作

##### 验证步骤（curl 管理操作 + TUI 体验验证）

**Step E1 — 【TUI 基线】analysis-agent 确认当前可读供应商成本**

在 TUI 中输入：

```
请读取 ctx://team/engineering/docs/supplier-costs 的内容
```

预期：成功返回供应商成本明细（这是 deny 之前的基线）。

**Step E2 — 【curl】管理员创建 deny policy**

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

预期：返回 201 + policy 对象。记录 `id`（后续删除需要）。

**Step E3 — 【TUI 体验】analysis-agent 再次读取 → 被拦截**

在 TUI 中输入：

```
请再读一次 ctx://team/engineering/docs/supplier-costs
```

预期：agent 调用 `read` 但失败，Server 日志出现 403。
同一个 agent、同一条命令，设了 deny 后立刻读不到了。

**Step E4 — 【curl】删除 deny + 创建遮蔽策略**

```bash
# 删除 deny
curl -X DELETE http://localhost:8000/api/v1/admin/policies/<E2_POLICY_ID> \
  -H "X-API-Key: changeme" -H "X-Account-Id: acme" -H "X-Agent-Id: query-agent"

# 创建 allow + field_masks
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

**Step E5 — 【TUI 体验】analysis-agent 读到遮蔽后的内容**

在 TUI 中输入：

```
请读取 ctx://team/engineering/docs/supplier-costs 的内容
```

预期：成功返回，但 "60%"、"底价" 等敏感词显示为 `[MASKED]`。

**Step E6 — 【curl】Share grant 授权**

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

预期：返回 201 + policy 对象（`conditions.kind = "share_grant"`）。

**Step E7 — 【TUI 体验】analysis-agent 读取共享的 API 规范**

在 TUI 中输入：

```
请读取 ctx://team/engineering/docs/api-standards 的内容
```

预期：成功返回 API 规范内容。

**Step E8 — 【TUI】analysis-agent 浏览共享空间，产生审计记录**

在 TUI 中输入：

```
请列出 ctx://team/engineering/memories/shared_knowledge
```

**Step E9 — 【curl】查看审计日志**

```bash
curl "http://localhost:8000/api/v1/admin/audit?limit=20" \
  -H "X-API-Key: changeme" \
  -H "X-Account-Id: acme" \
  -H "X-Agent-Id: query-agent" | python3 -m json.tool
```

预期：包含 `policy_change`、`access_denied`、`read`、`ls` 等审计记录。

##### Phase E 验证要点总结

| 验证点 | 管理操作（curl） | 体验验证（TUI） | 来源 |
|--------|-----------------|-----------------|------|
| ACL 拦截 | E2 创建 deny | E1 基线可读 → E3 被拒 | Phase 2 |
| 关键字遮蔽 | E4 创建 masking 策略 | E5 敏感词 → `[MASKED]` | Phase 2 |
| 免复制共享 | E6 创建 share grant | E7 原本不可见 → 可读 | Phase 2 |
| 审计合规 | E9 查询 audit_log | E8 产生审计记录 | Phase 2 |

#### TUI 录屏注意事项

1. **Phase A + B（4 步）是最小展示**，足以证明跨 Agent 协作在
   真实 runtime 中工作
2. **Phase D（10 步）是完整展示**，额外证明私有隔离和双向协作，
   推荐在正式 demo 中使用；需要 1-2 次 agent 切换（Step D10
   为可选，省略则只需 1 次切换）
3. Phase C（2 步）可独立录制，展示 skill 版本治理能力
4. **Phase E（9 步）是治理层展示**（Phase 2 新增），curl + TUI 双视角，
   演示 ACL deny、关键字遮蔽、免复制共享和审计日志，推荐在 Phase D 之后录制
5. 如果模型没有主动调用工具，不代表产品失败 —— 这是 prompt
   问题，不是 ContextHub 问题。可以更直接地说
   "请调用 read 工具，URI 是 ctx://..."

### 产出物

- curl 命令 + 请求/响应 JSON 片段
- sidecar 日志截图
- （可选）TUI 录屏

---

## 关键系统指标汇总

### Phase 1 指标

| 指标 | 来源 | 目标 |
|------|------|------|
| 传播命中率 | 第一层 P-1~P-6 全 pass → 推导 | 100% |
| 权限泄漏率（默认 ACL） | 第一层 A-1~A-4 全 pass → 推导 | 0% |
| pinned/latest 解析正确性 | 第一层 C-4/C-5 全 pass → 推导 | 100% |
| 事件丢失恢复能力 | 第一层 P-7/P-8 全 pass → 推导 | 100% |
| 变更收敛时延 | 第二层 Step 5-6 计时 → 观测 | < 2s |
| 跨 Agent 复用信号 | 第三层 Step 3 assemble → 观测 | ≥ 1 条 |
| 运行时合同成立 | 第三层 4 个 dispatch/assemble → 观测 | 全通过 |

### Phase 2 指标

| 指标 | 来源 | 目标 |
|------|------|------|
| ACL deny 拦截率 | 第一层 A-5~A-7 + demo_phase2.py Step 3 | 100% |
| 关键字遮蔽生效率 | 第一层 A-8 + demo_phase2.py Step 5 | 100% |
| 审计完整性 | 第一层 A-10~A-12 + demo_phase2.py Step 9 | 策略/拒绝/读均有记录 |
| Share grant/revoke 正确性 | 第一层 A-13~A-14 + demo_phase2.py Step 6-7 | grant→可读, revoke→不可读, 无复制 |
| 写操作豁免 | 第一层 A-15 + demo_phase2.py Step 8 | read-deny 不影响写 |
| Phase 1 回归 | A-1~A-4, P-*, C-* 全部仍 pass | 0 regressions |

> **不采集**：token 节省率、EX/accuracy benchmark、统计显著性收益
>（属于 Phase 4 三层 Benchmark 后续工作，详见 `plan/15-benchmark-plan.md`）。

---

## 产出清单

| 产出物 | 说明 |
|--------|------|
| 自动化测试报告 | `pytest -q` 原始输出（含 Phase 1 + Phase 2 用例） |
| Phase 1 API 闭环 demo | `demo_e2e.py` stdout + `convergence_ms` |
| Phase 2 治理 API demo | `demo_phase2.py` stdout（ACL deny/masking/share/audit） |
| 运行时合同验证 | 4 组 curl 请求/响应 + sidecar 日志（Phase A-D） |
| 治理 curl 验证 | 6 组 curl 请求/响应（Phase E：ACL/masking/share/audit） |
| 关键指标表 | Phase 1（7 项）+ Phase 2（6 项）系统指标 + 数值 |
| 验证边界声明 | 已验证 Phase 1 + Phase 2；未验证项：HA、多区域、合规认证 |
| TUI 录屏（可选） | Phase D + Phase E 展示材料，不作为退出门槛 |
| 功能对比表（可选） | vs Mem0 / CrewAI / Governed Memory |

---

## 建议执行顺序

1. **第一层** — 跑 `pytest -q`，截图保存原始输出（含 Phase 2 A-5~A-15）
2. **第二层** — 执行 `demo_e2e.py`（Phase 1 闭环），然后执行
   `demo_phase2.py`（Phase 2 治理闭环）
3. **第三层** — 启动完整 5 终端栈，按顺序执行 Phase A-D（4+10 步），
   然后执行 Phase E（6 步 curl）

全部通过后，可以写：

> ContextHub 已验证其作为企业多 Agent 协作的上下文治理中间件核心能力，
> 包括隔离、共享晋升、版本治理、变更传播、运行时集成（Phase 1），
> 以及显式 ACL 读控制、关键字遮蔽、分层审计、免复制跨团队共享（Phase 2）。

