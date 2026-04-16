# 16 — AMC 接入 OpenClaw Plugin / Context Engine 实施方案

## 16.1 目标与范围

目标：将 AMC 从“独立 commit/retrieve 服务”演进为 OpenClaw 的 **context engine plugin**，使 AMC 直接参与每轮上下文构建生命周期。

本方案聚焦：
- 以 `api.registerContextEngine(...)` 接入 OpenClaw；
- 将 AMC 现有 commit/retrieve 能力映射到 context-engine 生命周期；
- 明确分阶段落地、测试与验收标准。

本方案不覆盖：
- OpenClaw 核心 capability 扩展（除 context-engine 所需最小面）；
- UI/前端配置页面设计；
- 生产运维体系（告警平台、自动扩缩容）细节。

---

## 16.2 OpenClaw 对接契约（最小实现）

AMC plugin 需要在 `register(api)` 时注册：

```ts
api.registerContextEngine("amc", factory)
```

并在配置中选择：

```json
{
  "plugins": {
    "slots": {
      "contextEngine": "amc"
    }
  }
}
```

Context engine 最小必需接口：
- `info`
- `ingest(params)`
- `assemble(params)`
- `compact(params)`

建议实现的可选接口：
- `bootstrap(params)`：会话初始化
- `ingestBatch(params)`：按回合批量 ingest（更贴合 AMC）
- `afterTurn(params)`：回合后异步任务
- `onSubagentEnded(params)`：子会话清理
- `dispose()`：资源释放

---

## 16.3 AMC 能力到生命周期映射

### A. ingest / ingestBatch

输入：新消息（单条或一轮）

AMC 动作：
1. 规范化 message -> step（保留 role、action、action_result）；
2. 写入 session 级暂存区；
3. `ingestBatch` 时触发 commit 流程：
   - pair AI/Tool
   - build raw/clean graph
   - summarize L0/L1
   - 写 FS + Neo4j + pgvector metadata。

输出：
- `{ ingested: true }`

### B. assemble

输入：当前 session 消息 + token budget

AMC 动作：
1. 构造 retrieve query（task + recent context + constraints）；
2. 语义召回（pgvector）：
   - SQL 先按 `account_id/scope/owner_space/status` 过滤；
   - 再向量相似度排序；
   - 应用层同口径兜底复核；
3. 可选图召回（存在 partial trajectory 时）；
4. ACL 过滤；
5. 组装返回消息（evidence 可折叠到 system prompt addition）。

输出：
- `messages`
- `estimatedTokens`
- 可选 `systemPromptAddition`

### C. compact

Phase A（推荐起步）：
- `ownsCompaction = false`
- `compact()` 内部委托 OpenClaw 运行时默认压缩。

Phase B（AMC 接管）：
- `ownsCompaction = true`
- AMC 自定义压缩：
  - 保留最近窗口；
  - 老历史压缩为结构化 summary；
  - 保留可复用 workflow evidence。

### D. afterTurn

可放置异步任务：
- 质量分更新（adopted/ignored/corrected）；
- stale/lifecycle 更新；
- re-index 检查；
- 可选 promote 建议（agent -> team）。

---

## 16.4 插件内部分层（建议）

建议新增适配层，避免 OpenClaw 生命周期直接耦合现有 AMC 业务实现。

```text
amc_openclaw_plugin/
  index.ts                 # register(api)
  engine.ts                # ContextEngine 实现
  services/
    ingest_service.ts
    assemble_service.ts
    compact_service.ts
    after_turn_service.ts
  adapters/
    amc_commit_adapter.ts
    amc_retrieve_adapter.ts
    acl_adapter.ts
  config/
    schema.ts
```

核心原则：
- OpenClaw hook 层仅做编排；
- AMC 业务逻辑继续由现有 orchestrator/service 复用；
- 新旧入口（HTTP/CLI 与 plugin）共享同一核心逻辑。

---

## 16.5 配置设计（最小集）

`plugins.entries.amc` 建议字段：
- `enabled`
- `storage`：fs root / pgvector dsn / neo4j
- `retrieve`：top_n、graph_recall_on、acl_mode
- `compact`：`delegate | amc`
- `scope_defaults`：默认 `scope/owner_space` 策略

约束：
- `account_id` 必须来自 OpenClaw runtime context；
- `scope/owner_space` 不允许由用户 prompt 覆盖越权。

---

## 16.6 与现有 AMC API 的关系

现有接口：
- `POST /api/v1/amc/commit`
- `POST /api/v1/amc/retrieve`
- `POST /api/v1/amc/promote`

plugin 化后的建议：
1. 保留现有 API（用于独立调试与回归）；
2. OpenClaw plugin 走同一组 core orchestrator；
3. 通过 adapter 统一 request context 映射：
   - OpenClaw session/account -> AMC `account_id`
   - channel/scope context -> AMC `scope/owner_space`

---

## 16.7 v0 最小化连通性实现（先观察插件接入是否正常）

目的：在不引入 AMC 召回复杂度的前提下，先验证 OpenClaw plugin 生命周期触发、配置挂载与落盘链路。

### v0 行为定义

1. **仅实现 3 个 hook**
   - `ingest(params)`
   - `assemble(params)`
   - `compact(params)`

2. **compact 直接 delegate**
   - `ownsCompaction = false`
   - `compact()` 内部调用 OpenClaw runtime 默认压缩（不做 AMC 自定义压缩）

3. **ingest 只做原样消息记录**
   - 不做 pair/graph/summarize/index
   - 将收到的整条 message 原样写入本地目录：
     - `openclaw_message/`
   - 建议目录组织：
     - `openclaw_message/{session_id}/{timestamp}_{seq}.json`
   - 每条记录建议字段：
     - `session_id`
     - `account_id`（若可从上下文拿到）
     - `received_at`
     - `message`（完整原文对象，不裁剪）
     - `source`（`ingest`）

4. **assemble 返回空上下文**
   - `messages: []`
   - `estimatedTokens: 0`
   - `systemPromptAddition` 不返回

### v0 验收标准

- [ ] OpenClaw 启动后可识别 `contextEngine=amc`；
- [ ] 每次会话有新消息时，`openclaw_message/` 下新增记录文件；
- [ ] `assemble` 可被调用且返回空结构，不导致运行崩溃；
- [ ] `/compact` 可执行且由默认 runtime 行为处理；
- [ ] 切回 `legacy` 后行为可恢复。

### v0 退出条件（进入下一阶段）

满足以下条件即可进入 Phase 1：
- 生命周期 hook 触发稳定（至少连续 50 次 turn 无异常）；
- 落盘记录完整且可回放检查；
- 无权限越界字段（scope/owner_space）注入问题；
- 插件加载/卸载和配置切换无残留状态。

---

## 16.8 分阶段实施计划

### Phase 1（最小可用）
- 注册 `contextEngine=amc`
- 实现 `ingest` + `assemble` + `compact(delegate)`
- assemble 仅语义召回
- 验收：基础对话不中断，能注入 AMC 历史上下文

### Phase 2（能力对齐）
- 接入 `ingestBatch` + `afterTurn`
- 复用现有 commit 全链路（raw/clean/summary/index）
- assemble 增加图召回分支
- 验收：召回质量接近现有 AMC CLI/API

### Phase 3（增强）
- `ownsCompaction=true`，AMC 自定义压缩
- 反馈闭环、生命周期、promote 策略联动
- 验收：长会话 token 稳定、召回质量不下降

---

## 16.9 测试与验收

### 单元测试
- engine hooks 输入输出契约：
  - `ingest` 返回结构
  - `assemble` 消息顺序与 token 估算
  - `compact` delegate / amc 两模式

### 集成测试
- OpenClaw session -> AMC commit/retrieve 贯通；
- account/scope/owner_space 隔离；
- subagent 结束清理。

### 回归测试
- 现有 `scripts/test_commit_api.py`、`scripts/test_retrieve_api.py`、`scripts/test_promote_api.py` 全通过；
- `sample_traj` 新结构（`query + trajectory`）在 plugin 路径可用。

---

## 16.10 风险与缓解

1. **生命周期不匹配**
   - 风险：OpenClaw 回调频率与 AMC commit 粒度不一致
   - 缓解：优先 `ingestBatch`，并做轻量缓存聚合

2. **时延上升**
   - 风险：assemble 中检索耗时过高
   - 缓解：先语义 topN 收缩，再图匹配；设置超时与降级

3. **权限穿透**
   - 风险：scope/owner_space 映射错误造成越权召回
   - 缓解：双层校验（SQL 过滤 + ACL 最终裁决）

4. **双入口行为漂移**
   - 风险：HTTP 路径与 plugin 路径结果不一致
   - 缓解：统一 core orchestrator，adapter 仅做 context 映射

---

## 16.11 交付清单（Done 定义）

- [ ] 新增 `amc` context engine plugin，能被 OpenClaw 正常加载；
- [ ] `plugins.slots.contextEngine=amc` 时可完成 ingest/assemble/compact；
- [ ] assemble 接入 AMC semantic recall（并保留 ACL 过滤）；
- [ ] commit/retrieve/promote 现有 API 回归通过；
- [ ] 文档补齐：安装、配置、故障排查与回退（切回 `legacy`）。

---

## 16.12 参考主分支 ContextHub 接入实现（仿照清单）

本节基于 main 分支已存在的 ContextHub -> OpenClaw 接入代码（`bridge/`）提炼 AMC 可复用模式，作为后续实现模板。

### 16.12.1 已验证的实现模式

1. **双层桥接（推荐）**
   - OpenClaw 插件层：TypeScript（运行在 OpenClaw 进程内）
   - 业务执行层：Python sidecar（HTTP）
   - 优点：不强耦合 OpenClaw monorepo，AMC 现有 Python 资产可复用。

2. **插件只做编排，不承载重逻辑**
   - TS `index.ts` 仅 `registerContextEngine(...)` +（可选）`registerTool(...)`
   - 真正的 ingest/assemble/afterTurn 逻辑在 sidecar 后端。

3. **compact 默认 delegate**
   - `ownsCompaction=false`
   - TS bridge 中 `compact()` 动态 import SDK 后调用 `delegateCompactionToRuntime(...)`
   - 与我们 v0 目标完全一致。

4. **manifest + package.json 双声明**
   - `openclaw.plugin.json`：声明 `id/kind/configSchema`
   - `package.json`：声明 `openclaw.extensions=["./dist/index.js"]`
   - 缺一会导致 OpenClaw 插件安装/加载异常。

5. **多 agent 支持通过请求头传递**
   - sidecar 使用 `X-Agent-Id` 作为 engine/client 实例路由键
   - 便于后续 AMC 做 agent 级隔离调试。

### 16.12.2 AMC 插件目录建议（按 bridge 结构对齐）

```text
amc_bridge/
  openclaw.plugin.json
  package.json
  tsconfig.json
  src/
    index.ts         # register(api)
    bridge.ts        # AMCBridge: ContextEngine hook -> HTTP sidecar
    sidecar.py       # FastAPI sidecar: ingest/assemble/compact endpoints
```

### 16.12.3 关键文件模板要求

#### A) `openclaw.plugin.json`

最小建议：
- `id`: `"amc"`
- `kind`: `"context-engine"`
- `configSchema.properties.sidecarUrl`（默认例如 `http://localhost:9200`）

#### B) `package.json`

关键字段：
- `"name"` 与插件 id 语义一致（建议 `amc`）
- `"main": "dist/index.js"`
- `"type": "module"`
- `"openclaw.extensions": ["./dist/index.js"]`
- `scripts.build = tsc`

#### C) `src/index.ts`

最小行为：
- 读取 `api.pluginConfig.sidecarUrl`
- `api.registerContextEngine("amc", () => new AMCBridge(sidecarUrl))`
- v0 阶段不注册任何 AMC tools（可后续增量加入）。

#### D) `src/bridge.ts`

实现 hook 转发：
- `ingest(params) -> POST /ingest`
- `assemble(params) -> POST /assemble`
- `compact(params) -> delegateCompactionToRuntime(params)`

`info` 固定：
- `id: "amc"`
- `ownsCompaction: false`

#### E) `src/sidecar.py`

提供最小端点：
- `POST /ingest`
- `POST /assemble`
- `GET /health`

v0 行为必须与 16.7 对齐：
- ingest：落盘到 `openclaw_message/`
- assemble：返回空消息
- compact：不在 sidecar 实现（由 TS bridge delegate）

### 16.12.4 v0 端点契约（建议固定）

#### `POST /ingest` 请求体

```json
{
  "sessionId": "string",
  "sessionKey": "string|null",
  "message": {},
  "isHeartbeat": false
}
```

响应：

```json
{ "ingested": true }
```

#### `POST /assemble` 请求体

```json
{
  "sessionId": "string",
  "messages": [],
  "tokenBudget": 0
}
```

响应（v0）：

```json
{
  "messages": [],
  "estimatedTokens": 0
}
```

### 16.12.5 与 `docs/openclaw-integration-guide.md` 对齐项

后续 AMC 集成文档应复用其操作路径：
1. `npm install && npm run build`（bridge）
2. `openclaw plugins install -l <bridge_path>`
3. `plugins.slots.contextEngine = "amc"`
4. 启 sidecar + OpenClaw gateway + TUI
5. 用 `/health` 与最小 `assemble` 请求先验证连通性

### 16.12.6 先不做（避免过早复杂化）

- v0 阶段不接入 AMC commit/retrieve/promotion 主流程；
- v0 阶段不注册工具集合；
- v0 阶段不实现 ownsCompaction=true；
- v0 阶段不引入 Neo4j/pgvector 依赖启动链路。

