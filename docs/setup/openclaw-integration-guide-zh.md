# ContextHub + OpenClaw 端到端集成指南

> 将 ContextHub 作为 context engine 接入 OpenClaw agent 运行时的完整步骤。
> 全部本地运行，不需要 Docker。

## 总览

完整运行需要 5 个终端：

```
┌──────────────────────────────────────────────────────────────┐
│  终端 1: PostgreSQL          (Homebrew 后台服务，常驻)         │
│  终端 2: ContextHub Server   (FastAPI，端口 :8000)            │
│  终端 3: Python Sidecar      (FastAPI，端口 :9100)            │
│  终端 4: OpenClaw Gateway    (Node.js，端口 :18789)           │
│  终端 5: OpenClaw TUI        (交互式 agent 对话)              │
└──────────────────────────────────────────────────────────────┘
```

数据流：

```
OpenClaw TUI（你输入）──► OpenClaw Gateway
  └─ ContextHubBridge（TS，context-engine 插件）
       └─ HTTP ──► Python Sidecar (:9100)
            └─ ContextHubContextEngine（Python 插件）
                 └─ ContextHubClient（SDK）
                      └─ ContextHub Server (:8000)
                           └─ PostgreSQL + pgvector
```

## 前置依赖

| 依赖           | 版本     | 安装方式                         |
| -------------- | -------- | -------------------------------- |
| macOS + Homebrew | —       | https://brew.sh                  |
| Python         | 3.11+    | `brew install python@3.12`       |
| Node.js        | ≥ 22.12  | `brew install node`              |
| pnpm           | 9+       | `npm install -g pnpm`            |
| PostgreSQL     | 16       | `brew install postgresql@16`     |
| pgvector       | —        | `brew install pgvector`          |

还需要一个 **LLM API key**（Anthropic 或 OpenAI），供 OpenClaw agent 调用模型。

---

## 一次性设置（首次运行前完成）

### 第 1 步：安装 PostgreSQL + pgvector

```bash
brew install postgresql@16
brew install pgvector
brew services start postgresql@16
```

验证：

```bash
pg_isready
# 预期输出："accepting connections"
```

### 第 2 步：创建数据库

```bash
psql postgres -c "CREATE USER contexthub WITH PASSWORD 'contexthub' SUPERUSER;"
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
```

> **说明**：`SUPERUSER` 是因为 schema 使用了 `FORCE ROW LEVEL SECURITY`。本地开发环境没问题。

### 第 3 步：创建 Python 虚拟环境并安装依赖

```bash
cd /path/to/ContextHub

# 如果激活了 conda，先退出
conda deactivate

python3 -m venv .venv
source .venv/bin/activate

# 安装所有依赖
pip install -e ".[dev]"
pip install greenlet
pip install -e sdk/
pip install -e plugins/openclaw/
```

### 第 4 步：运行数据库迁移

```bash
cd /path/to/ContextHub
source .venv/bin/activate
alembic upgrade head
```

预期输出：

```
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema...
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, Force row level security
```

### 第 5 步：编译 TypeScript Bridge

```bash
cd /path/to/ContextHub/bridge
npm install
npm run build
```

编译成功后 `bridge/dist/` 下会生成 `index.js`、`bridge.js`、`tools.js` 等文件。

### 第 6 步：克隆并编译 OpenClaw

```bash
git clone https://github.com/openclaw/openclaw.git /path/to/public/openclaw
cd /path/to/public/openclaw

# 如果 pnpm install 报 SSH / host key 错误，先执行：
git config url."https://github.com/".insteadOf "git@github.com:"

pnpm install
pnpm ui:build
pnpm build
```

> **注意**：`/path/to/public/openclaw` 是 **OpenClaw 主仓库**（一个 Node.js 项目，
> 包含 `package.json`）。请勿与 `ContextHub/plugins/openclaw/` 混淆——后者是一个
> Python 插件包。
>
> **注意**：`pnpm ui:build` 仅在 OpenClaw **≥ v2026.4.x** 时需要。如果使用旧版本
>（如 v2026.3.14），可以跳过此步——直接 `pnpm install && pnpm build` 即可。

### 第 7 步：将 ContextHub 插件安装到 OpenClaw

```bash
cd /path/to/public/openclaw
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

预期输出（警告信息可忽略）：

```
Exclusive slot "contextEngine" switched from "legacy" to "contexthub".
Linked plugin path: ~/path/to/ContextHub/bridge
Restart the gateway to load plugins.
```

### 第 8 步：配置 OpenClaw 的 LLM 模型

```bash
cd /path/to/public/openclaw
pnpm openclaw configure
```

按交互提示配置 API key（Anthropic / OpenAI）。

也可以直接编辑 `~/.openclaw/openclaw.json`：

```json5
{
  "plugins": {
    "slots": {
      "contextEngine": "contexthub"
    },
    "entries": {
      "contexthub": {
        "enabled": true,
        "config": {
          "sidecarUrl": "http://localhost:9100"
        }
      }
    }
  }
}
```

### 注意：如果intergration翻车了，需要洗掉数据库里已存的上下文的话，可以这样执行：
```bash
# 删库
dropdb contexthub
# 重建库（指定 owner）
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
# 重建 extensions
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
# 重建 schema + seed 数据
cd /path/to/ContextHub
source .venv/bin/activate
alembic upgrade head
```

另外需要清掉OpenClaw 的会话历史（不然OpenClaw又会基于会话历史自由发挥了）
在 ~/.openclaw/agents/main/sessions/ 下面
```bash
# 先 Ctrl+C 停掉 TUI 和 Gateway，然后：
rm -rf ~/.openclaw/agents/main/sessions/*
```

---

## 日常启动流程

完成一次性设置后，每次使用按以下顺序启动。

### 终端 1 — PostgreSQL

PostgreSQL 作为 Homebrew 后台服务运行，开机自动启动。确认状态：

```bash
pg_isready
# 预期："accepting connections"
```

如果没有运行：

```bash
brew services start postgresql@16
```

### 终端 2 — ContextHub Server

```bash
cd /path/to/ContextHub
conda deactivate
source .venv/bin/activate
uvicorn contexthub.main:app --port 8000
```

**保持此终端打开。** 在另一个窗口验证：

```bash
curl http://localhost:8000/health
# 预期：{"status":"ok"}
```

> **提示**：如果没有在 `.env` 中设置 `OPENAI_API_KEY`，服务器会使用 NoOp embedding
> 客户端。向量搜索结果会有限——这在本地 MVP 验证中是正常的。

### 终端 3 — Python Sidecar

```bash
cd /path/to/ContextHub
source .venv/bin/activate
python bridge/src/sidecar.py \
  --port 9100 \
  --contexthub-url http://localhost:8000 \
  --agent-id query-agent \
  --account-id acme
```

注：启动 sidecar 时可加上环境变量，关闭 auto-capture
* 对 demo 来说，验证的是用户主动 store/promote 的链路，auto-capture 产生的噪音记忆反而干扰验证。
* 对生产场景来说：auto-capture 的初衷是让 agent 在对话中自动积累知识，但目前的实现比较粗糙（_looks_reusable 启发式规则会把 tool call ID、URI 等误判为"可复用事实"），关掉也没什么损失。等启发式规则完善了再默认开启更合理。

```bash
CONTEXTHUB_AUTO_CAPTURE=off python bridge/src/sidecar.py --port 9100 \
  --contexthub-url http://localhost:8000 --agent-id query-agent --account-id acme
```

**保持此终端打开。** 验证：

```bash
curl http://localhost:9100/health
# 预期：{"status":"ok"}

curl http://localhost:9100/tools
# 预期：包含 7 个 tool 定义的 JSON 数组
```

### 终端 4 — OpenClaw Gateway

```bash
cd /path/to/public/openclaw
pnpm openclaw gateway
```

**保持此终端打开。** Gateway 会在端口 18789 启动。
观察日志中是否有 ContextHub 插件加载的信息。

### 终端 5 — OpenClaw TUI（交互对话）

```bash
cd /path/to/public/openclaw
pnpm openclaw tui
```

现在可以和 agent 对话了。幕后发生的事情：

- **每次提问**：`assemble()` 自动从 ContextHub 检索相关上下文，
  通过 `systemPromptAddition` 注入系统提示。
- **每次回复后**：`afterTurn()` 从助手回复中提取可复用的事实，
  写入 ContextHub 作为私有记忆。
- **Agent 工具**：agent 可以调用 `ls`、`read`、`grep`、`stat`、
  `contexthub_store`、`contexthub_promote`、`contexthub_skill_publish`。

---

## 验证集成是否正常

### 快速检查：插件是否加载？

```bash
cd /path/to/public/openclaw
pnpm openclaw doctor
```

在 context engine 部分查看是否显示 `contexthub`。

### 快速检查：sidecar 各端点

```bash
# 引擎信息
curl http://localhost:9100/info

# 模拟 assemble（auto-recall）
curl -X POST http://localhost:9100/assemble \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "test", "messages": [{"role": "user", "content": "orders 表的 join 条件是什么？"}]}'

# 模拟 tool dispatch
curl -X POST http://localhost:9100/dispatch \
  -H "Content-Type: application/json" \
  -d '{"name": "ls", "args": {"path": "ctx://"}}'
```

### 完整 ContextHub E2E 演示（不依赖 OpenClaw）

验证 ContextHub 服务器本身的多 agent 记忆、Skill、传播功能：

```bash
cd /path/to/ContextHub
source .venv/bin/activate
python scripts/demo_e2e.py
```

演示脚本覆盖 7 个步骤：

| 步骤 | 内容 |
|------|------|
| 1 | `query-agent` 写入私有记忆 |
| 2 | `query-agent` 创建并发布 Skill v1 |
| 3 | `query-agent` 将记忆晋升到 `team/engineering` |
| 4 | `analysis-agent` 检索共享记忆并订阅 Skill |
| 5 | `query-agent` 发布 breaking Skill v2 |
| 6 | 验证传播：stale/advisory 检测 |
| 7 | （垂直载体）Catalog 同步 + sql-context 查询 |

---

## 多 Agent 测试

Sidecar 支持通过 `X-Agent-Id` 请求头动态切换 agent 身份。
不同的 OpenClaw 会话可以表现为不同的 ContextHub agent。

也可以运行多个 sidecar 实例：

```bash
# 终端 3a
python bridge/src/sidecar.py --port 9100 --agent-id query-agent

# 终端 3b
python bridge/src/sidecar.py --port 9101 --agent-id analysis-agent
```

---

## 重置状态

### 重置 ContextHub 数据库

```bash
# 方式 A：只清除业务数据（保留种子数据）
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  TRUNCATE contexts, dependencies, change_events,
           table_metadata, lineage, table_relationships,
           query_templates, skill_versions, skill_subscriptions
  CASCADE;
"

# 方式 B：完全重建数据库（重新运行迁移 + 种子数据）
psql postgres -c "DROP DATABASE IF EXISTS contexthub;"
psql postgres -c "CREATE DATABASE contexthub OWNER contexthub;"
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
alembic upgrade head
```

### 重置 OpenClaw 插件

```bash
cd /path/to/public/openclaw
pnpm openclaw plugins disable contexthub   # 禁用
pnpm openclaw plugins enable contexthub    # 重新启用
```

切换回内置的 legacy context engine：

```bash
# 编辑 ~/.openclaw/openclaw.json
# 将 plugins.slots.contextEngine 改为 "legacy"（或删除该字段）
```

---

## 停止服务

| 服务              | 停止方式                                    |
| ----------------- | ------------------------------------------ |
| OpenClaw TUI      | 在终端 5 按 `Ctrl+C`                        |
| OpenClaw Gateway  | 在终端 4 按 `Ctrl+C`                        |
| Python Sidecar    | 在终端 3 按 `Ctrl+C`                        |
| ContextHub Server | 在终端 2 按 `Ctrl+C`                        |
| PostgreSQL        | `brew services stop postgresql@16`          |

---

## 常见问题排查

### `pnpm install` 报 SSH / host key 错误

```bash
cd /path/to/public/openclaw
git config url."https://github.com/".insteadOf "git@github.com:"
pnpm install
```

如果仓库级配置不生效（pnpm store 在其他路径），改为全局配置：

```bash
git config --global url."https://github.com/".insteadOf "git@github.com:"
```

完成后可恢复：

```bash
git config --global --unset url."https://github.com/".insteadOf
```

### `pnpm openclaw plugins install` 报 "missing openclaw.extensions"

确保 bridge 的 `package.json` 包含：

```json
"openclaw": {
  "extensions": ["./dist/index.js"]
}
```

### 插件 id mismatch 警告

确保 bridge 的 `package.json` 中 `"name"` 字段为 `"contexthub"`
（与 `openclaw.plugin.json` 中的 `id` 一致）。

### `unrecognized configuration parameter "app.account_id"`

数据库用户不是 superuser，需要修复：

```bash
psql postgres -c "ALTER USER contexthub WITH SUPERUSER;"
```

### Sidecar 连不上 ContextHub Server

确保在启动 sidecar 之前，server 已在 `:8000` 运行：

```bash
curl http://localhost:8000/health
```

### 服务器启动时出现传播错误（`Failed to update embedding`）

上一次运行残留的 `change_events` 在没有 embedding 客户端的情况下被处理。清除它们：

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  UPDATE change_events
  SET delivery_status = 'processed', processed_at = NOW()
  WHERE delivery_status IN ('pending', 'retry', 'processing');
"
```

或者在 `.env` 中禁用传播引擎：

```
PROPAGATION_ENABLED=false
```

### demo 第 2 步返回 403 Forbidden

`query-agent` 缺少 `engineering` 团队的成员身份。更新版的 demo 脚本会自动处理。
如果使用旧版脚本：

```bash
psql postgresql://contexthub:contexthub@localhost:5432/contexthub -c "
  SET app.account_id = 'acme';
  INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
  VALUES ('query-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE)
  ON CONFLICT DO NOTHING;
"
```

### `alembic` 或 `uvicorn` 用了 conda 的而不是 .venv 的

务必先退出 conda：

```bash
conda deactivate
source .venv/bin/activate
```

用 `which alembic` 验证——应该指向 `.venv/bin/alembic` 而不是 `/opt/anaconda3/bin/alembic`。

### OpenClaw Gateway 启动失败

检查 Node.js 版本是否 ≥ 22.12：

```bash
node --version
```

检查 OpenClaw 是否编译成功：

```bash
cd /path/to/public/openclaw
ls dist/index.js
```

---

## 架构参考

```
ContextHub/
├── src/contexthub/          # FastAPI 服务端（contexts、memories、skills、search、propagation）
├── sdk/                     # Python SDK（ContextHubClient）
├── plugins/openclaw/        # Python ContextEngine 插件（assemble、afterTurn、tools）
├── bridge/
│   ├── openclaw.plugin.json # OpenClaw 插件清单（kind: "context-engine"）
│   ├── src/
│   │   ├── index.ts         # 插件入口：register(api) → registerContextEngine + registerTool
│   │   ├── bridge.ts        # ContextHubBridge：TS 端 ContextEngine 实现 → HTTP → sidecar
│   │   ├── tools.ts         # 7 个 MVP 工具定义（ls/read/grep/stat/store/promote/publish）
│   │   └── sidecar.py       # Python HTTP 包装层（支持 X-Agent-Id 多 agent）
│   └── dist/                # 编译后的 JS（npm run build 之后生成）
├── scripts/demo_e2e.py      # 独立 E2E 演示脚本（不依赖 OpenClaw）
├── tests/                   # pytest 测试套件
├── alembic/                 # 数据库迁移
└── docker-compose.yml       # PostgreSQL 容器（可选，上面用 Homebrew 替代）
```
