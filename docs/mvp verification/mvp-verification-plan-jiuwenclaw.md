# ContextHub MVP 验证实施计划（JiuwenClaw 版）

> 本文档面向一个刚拿到仓库、从零开始启动的人。
> 目标：用最短路径验证 ContextHub 在 **JiuwenClaw + Python bridge**
> 运行时中的核心价值是否成立。
>
> **验证边界**：本次验证的是 ContextHub 的 MVP 内核能力：
> 私有隔离、团队共享晋升、技能版本治理、变更传播、运行时集成。
> 不包含 ACL、审计、HA、SSO、合规等 enterprise-ready 能力。

## 前置状态

| 组件 | 目标状态 |
|------|----------|
| PostgreSQL | 由 `docker compose up -d` 启动 |
| ContextHub Server | FastAPI 运行在 `127.0.0.1:8000` |
| Jiuwen bridge | 使用 `bridge_jiuwen` |
| JiuwenClaw 主应用 | 可选，不是本计划的硬依赖 |
| 自动化验证 | 以实际命令输出为准，不预写通过数 |

## 为什么 JiuwenClaw 版和 OpenClaw 版不同

原始 [mvp-verification-plan.md](./mvp-verification-plan.md)
针对的是 OpenClaw runtime。

本版本的差异只有一层：

- OpenClaw 运行时集成是 TypeScript plugin
- JiuwenClaw 运行时集成是 Python extension / Python sidecar

但为了让验证合同尽量一致，`bridge_jiuwen` 仍然暴露了与 OpenClaw 风格接近的 HTTP 接口：

- `POST /dispatch`
- `POST /assemble`
- `POST /after-turn`
- `GET /health`
- `GET /info`
- `GET /tools`

所以第三层的 4 个 curl 在 JiuwenClaw 版中仍然成立。

---

## 核心原则

1. **证治理内核，不证模型聪明程度**：要看 sidecar / server 返回的结构化内容，不要只看聊天窗口里模型自然语言回答得像不像。
2. **先证 Server，再证 runtime 合同**：第二层证明 ContextHub 内核闭环成立，第三层再证明 Jiuwen runtime 真正接上了。
3. **脚本优先，手工可复现**：先跑脚本得到确定性结论，再用 curl 或日志核对关键内容。

---

## 第一层：自动化功能正确性

这一层与 OpenClaw 版含义相同，证明的是 ContextHub Server 本身的正确性。

建议先在仓库根目录执行：

```bash
cd ContextHub
source .venv/bin/activate
pytest -q
```

重点关注三类能力：

| 能力 | 含义 |
|------|------|
| 变更传播 | breaking / non-breaking 更新是否正确扩散 |
| 多 Agent 协作 | promote、shared read、skill pinned/latest 是否成立 |
| 可见性与隔离 | agent 私有隔离、团队层级继承是否成立 |

### 验收标准

1. `pytest -q` 退出码为 0
2. 无权限泄漏类失败
3. 无多 Agent 共享/隔离回归

### 产出物

- 带日期的 `pytest -q` 原始输出

---

## 第二层：API 内核闭环 demo

目标：不依赖 JiuwenClaw 主应用，直接通过 ContextHub API 跑通核心故事。

这层主要靠：

- [demo_e2e.py](../../scripts/demo_e2e.py)

它负责预置：

- `query-agent` 的私有记忆
- `engineering` 团队共享上下文
- `sql-generator` skill context
- pinned subscription
- 后续 skill 版本演化验证所需的基线数据

### 从零开始启动

#### Terminal 1：启动 PostgreSQL

```bash
cd ContextHub
docker compose up -d
```

#### Terminal 2：启动 ContextHub Server

```bash
cd ContextHub
source .venv/bin/activate
alembic upgrade head
uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

#### Terminal 3：确认服务健康

```bash
curl http://127.0.0.1:8000/health
```

预期：

```json
{"status":"ok"}
```

### 跑闭环 demo

```bash
cd ContextHub
source .venv/bin/activate
python scripts/demo_e2e.py
```

### 验证点

| Step | 动作 | 预期 |
|------|------|------|
| 1 | query-agent 写私有记忆 | 返回 memory URI |
| 2 | 创建 skill context + 发布初始版本 | 返回 skill URI / version |
| 3 | promote 到 engineering | 返回 `ctx://team/engineering/...` |
| 4 | analysis-agent 建立 pinned read 路径 | 后续可读到旧版本 |
| 5 | query-agent 发布 breaking 新版本 | 返回新版本号 |
| 6 | analysis-agent 读取 skill | 返回 pinned 旧版本 + advisory |

### 验收标准

1. `python scripts/demo_e2e.py` 退出码为 0
2. 输出中出现 memory URI、promoted URI、skill version、advisory

### 产出物

- `demo_e2e.py` 的完整 stdout

---

## 第三层：JiuwenClaw 运行时合同验证

目标：证明 `Jiuwen bridge sidecar -> plugin engine -> ContextHub server`
这条链路真实成立。

### 这一层验证什么

这一层不是验证 JiuwenClaw UI 是否会“聪明地”决定调用工具，而是验证：

1. `dispatch(contexthub_store)` 能存
2. `dispatch(contexthub_promote)` 能晋升
3. `assemble()` 能把团队共享知识装配到 `systemPromptAddition`
4. `dispatch(contexthub_skill_publish)` 和 `dispatch(read)` 能验证 pinned + advisory

### 为什么不要求先启 JiuwenClaw 主应用

JiuwenClaw 真正的 hook 集成是 Python。
为了做可重复、可观察、可录屏的 MVP 验证，本计划优先使用
`bridge_jiuwen/src/sidecar.py`。

这样做的好处是：

- 不受聊天 UI 与 prompt 波动影响
- 每一步都能看到真实 JSON payload
- 能和原 OpenClaw 版的 runtime 合同保持近似一致

### 启动 sidecar

#### Terminal 4：启动 query-agent sidecar

```bash
cd ContextHub
source .venv/bin/activate
python bridge_jiuwen/src/sidecar.py \
  --port 9100 \
  --contexthub-url http://127.0.0.1:8000 \
  --api-key changeme \
  --account-id acme \
  --agent-id query-agent
```

#### Terminal 5：启动 analysis-agent sidecar

```bash
cd ContextHub
source .venv/bin/activate
python bridge_jiuwen/src/sidecar.py \
  --port 9101 \
  --contexthub-url http://127.0.0.1:8000 \
  --api-key changeme \
  --account-id acme \
  --agent-id analysis-agent
```

### Sidecar 健康检查

```bash
curl http://127.0.0.1:9100/health
curl http://127.0.0.1:9101/health
```

预期：

```json
{"status":"ok"}
```

---

## 第三层手工验证：4 个 curl

### Step 1：dispatch -> `contexthub_store`

```bash
curl -X POST http://127.0.0.1:9100/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "name": "contexthub_store",
    "args": {
      "content": "月度销售额查询要 JOIN orders 和 products 并按月份聚合",
      "tags": ["sql", "monthly-sales"]
    }
  }'
```

预期：

- 返回 `result`
- `result` 内可解析出新的 memory URI
- 内容里能看到刚写入的 SQL pattern

### Step 2：dispatch -> `contexthub_promote`

把 Step 1 的 URI 代入：

```bash
curl -X POST http://127.0.0.1:9100/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "name": "contexthub_promote",
    "args": {
      "uri": "<STEP1_MEMORY_URI>",
      "target_team": "engineering"
    }
  }'
```

预期：

- 返回新的 team URI
- URI 形如 `ctx://team/engineering/...`

### Step 3：assemble -> 验证自动召回

```bash
curl -X POST http://127.0.0.1:9101/assemble \
  -H "Content-Type: application/json" \
  -d '{
    "sessionId": "verify-001",
    "messages": [
      {"role": "user", "content": "月度销售额应该怎么查？"}
    ],
    "tokenBudget": 1024
  }'
```

预期：

- 返回 `systemPromptAddition`
- `systemPromptAddition` 非空
- 其中包含 promote 后的 SQL pattern
- 内容中能看到 `JOIN orders` / `products` / 月度聚合相关信息

> **注意**：这一层看的是 `systemPromptAddition`，不是看模型最终回复像不像答案。
> 如果这里非空且包含正确上下文，说明 runtime 集成成立。

### Step 4：dispatch -> `contexthub_skill_publish` + `read`

先由 `query-agent` 发布 breaking 新版本：

```bash
curl -X POST http://127.0.0.1:9100/dispatch \
  -H "Content-Type: application/json" \
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

然后由 `analysis-agent` 读取：

```bash
curl -X POST http://127.0.0.1:9101/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "name": "read",
    "args": {
      "uri": "ctx://team/engineering/skills/sql-generator"
    }
  }'
```

预期：

- publish 返回新版本号
- read 返回 `version: 1`
- read 同时返回 advisory，提示有更新版本可用
- advisory 形如 `vN available, currently pinned to v1`

---

## 推荐方式：直接运行 4-curl 验证脚本

如果你不想手工复制 4 个 curl，直接运行：

```bash
cd ContextHub
source .venv/bin/activate
python bridge_jiuwen/scripts/run_mvp_verification_curl.py
```

对应脚本：

- [run_mvp_verification_curl.py](../../bridge_jiuwen/scripts/run_mvp_verification_curl.py)

这个脚本会自动：

1. 检查 `ContextHub` 健康状态
2. 停掉已有 `bridge_jiuwen` sidecar
3. 运行 `demo_e2e.py` 作为 seed
4. 启动 query / analysis 两个 sidecar
5. 顺序执行 4 个验证请求
6. 打印每一步的真实 JSON 内容

### 预期输出要点

你应该看到类似：

```text
[PASS] Step 1: stored memory ...
[DATA] Step 1 response: ...
[PASS] Step 2: promoted to ctx://team/engineering/...
[DATA] Step 2 response: ...
[PASS] Step 3: assemble returned a systemPromptAddition ...
[DATA] Step 3 response: ...
[PASS] Step 4: publish + read returned pinned v1 with advisory ...
[DATA] Step 4 read response: ...
```

### 第三层验收标准

以下 4 项全部通过：

1. `dispatch(contexthub_store)` 成功返回 memory
2. `dispatch(contexthub_promote)` 成功返回 team URI
3. `assemble()` 返回非空 `systemPromptAddition`
4. `dispatch(contexthub_skill_publish)` + `dispatch(read)` 返回 pinned + advisory

---

## 可选：JiuwenClaw 主应用联调

如果你还想额外验证 JiuwenClaw 主应用本身是否正常，可另开终端：

### Terminal 6：JiuwenClaw app

```bash
systemctl --user restart jiuwenclaw-app
journalctl --user -u jiuwenclaw-app -f
```

### Terminal 7：JiuwenClaw web

```bash
systemctl --user restart jiuwenclaw-web
journalctl --user -u jiuwenclaw-web -f
```

浏览器打开：

```text
http://127.0.0.1:19001
```

> 这一步是展示性联调，不是 MVP 退出门槛。
> 本计划的核心证据仍然是前三层，尤其是第三层的 4-curl runtime 合同验证。

---

## 最终退出标准

当以下三项全部满足时，可认为 JiuwenClaw 版 ContextHub MVP 验证完成：

1. `pytest -q` 通过
2. `python scripts/demo_e2e.py` 通过
3. `python bridge_jiuwen/scripts/run_mvp_verification_curl.py` 通过

---

## 建议保留的证据材料

- `pytest -q` 原始输出
- `python scripts/demo_e2e.py` 原始输出
- `python bridge_jiuwen/scripts/run_mvp_verification_curl.py` 原始输出
- Terminal 2 / 4 / 5 的日志截图
- Step 3 `systemPromptAddition` 的完整 JSON 截图
- Step 4 pinned + advisory 的完整 JSON 截图

---

## 相关文件

- 原始 OpenClaw 版计划：
  [mvp-verification-plan.md](./mvp-verification-plan.md)
- Jiuwen bridge：
  [bridge_jiuwen/README.md](../../bridge_jiuwen/README.md)
- 4-curl 验证脚本：
  [run_mvp_verification_curl.py](../../bridge_jiuwen/scripts/run_mvp_verification_curl.py)
- API seed 脚本：
  [demo_e2e.py](../../scripts/demo_e2e.py)
