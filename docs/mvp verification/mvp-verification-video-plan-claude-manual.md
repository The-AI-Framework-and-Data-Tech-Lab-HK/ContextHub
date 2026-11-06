# Claude Code + ContextHub Manual 10-Step Demo

This runbook is the manual, terminal-by-terminal version of the Claude demo.
Unlike `bridge_claude/scripts/run_mvp_video.sh`, this version lets you perform
each step yourself in separate Claude terminals.

It uses:

- `query-agent` terminal for `D1-D4`
- `analysis-agent` terminal for `D5-D9`
- back to `query-agent` terminal for `D10`

## 1. Start ContextHub

### Terminal 1: start Postgres

```bash
cd "path to ContextHub"
docker compose up -d
```

### Terminal 2: start ContextHub

```bash
cd "path to ContextHub"
python -m uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

Verify:

```bash
curl --noproxy '*' http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok"}
```

## 2. Clear old demo memory

### Terminal 3: clear demo cache

```bash
cd "path to ContextHub"
bridge_claude/scripts/clear_demo_cache.sh
```

Expected:

```text
Claude demo cache cleared and stale Claude demo processes stopped.
```

## 3. Prepare Claude MCP configs

Create one MCP config file per agent.

### Terminal 3: create `query-agent` MCP config

```bash
cat > /tmp/contexthub-query-agent.mcp.json <<'EOF'
{
  "mcpServers": {
    "contexthub": {
      "type": "stdio",
      "command": "python",
      "args": ["path to ContextHub/bridge_claude/src/server.py"],
      "env": {
        "CONTEXTHUB_URL": "http://127.0.0.1:8000",
        "CONTEXTHUB_API_KEY": "changeme",
        "CONTEXTHUB_ACCOUNT_ID": "acme",
        "CONTEXTHUB_AGENT_ID": "query-agent",
        "ALL_PROXY": "",
        "all_proxy": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost"
      }
    }
  }
}
EOF
```

### Terminal 3: create `analysis-agent` MCP config

```bash
cat > /tmp/contexthub-analysis-agent.mcp.json <<'EOF'
{
  "mcpServers": {
    "contexthub": {
      "type": "stdio",
      "command": "python",
      "args": ["path to ContextHub/bridge_claude/src/server.py"],
      "env": {
        "CONTEXTHUB_URL": "http://127.0.0.1:8000",
        "CONTEXTHUB_API_KEY": "changeme",
        "CONTEXTHUB_ACCOUNT_ID": "acme",
        "CONTEXTHUB_AGENT_ID": "analysis-agent",
        "ALL_PROXY": "",
        "all_proxy": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost"
      }
    }
  }
}
EOF
```

## 4. Start two Claude terminals

### Terminal 4: start `query-agent`

```bash
cd "path to ContextHub"
source bridge_claude/scripts/common.sh
load_jiuwen_deepseek_env
set_claude_deepseek_env
claude --mcp-config /tmp/contexthub-query-agent.mcp.json --strict-mcp-config
```

### Terminal 5: start `analysis-agent`

```bash
cd "path to ContextHub"
source bridge_claude/scripts/common.sh
load_jiuwen_deepseek_env
set_claude_deepseek_env
claude --mcp-config /tmp/contexthub-analysis-agent.mcp.json --strict-mcp-config
```

In each Claude terminal, run:

```text
/mcp
```

Expected:

- server `contexthub` is connected
- the following tools are visible:
  - `contexthub_store`
  - `contexthub_promote`
  - `ls`
  - `read`
  - `grep`
  - `stat`

## 5. Manual D1-D10

### Phase 1: `query-agent`

Use Terminal 4 for `D1-D4`.

#### D1

```text
请记住：春季促销活动规则 —— 满 300 减 50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间 4 月 1 日至 15 日。
```

Expected:

- Claude chooses `contexthub_store`
- a private memory is created under `ctx://agent/query-agent/memories/...`

#### D2

```text
请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
```

Expected:

- Claude chooses `contexthub_promote`
- a shared memory appears under `ctx://team/engineering/memories/shared_knowledge/...`

#### D3

```text
请再记住一条：供应商谈判备忘 —— 春季促销的供货底价不能低于零售价的 60%，这个底线不要对外透露。这条只留在我的私有空间，不要共享。
```

Expected:

- Claude chooses `contexthub_store`
- the note stays private to `query-agent`

#### D4

```text
请列出 ctx://agent/query-agent/memories 下的所有记忆，并读取每条记忆的内容。
```

Expected:

- exactly 2 memories are shown
- the promo rule from `D1`
- the private supplier note from `D3`

### Phase 2: `analysis-agent`

Use Terminal 5 for `D5-D9`.

#### D5

```text
请记住：上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
```

Expected:

- Claude chooses `contexthub_store`
- a private memory is created under `ctx://agent/analysis-agent/memories/...`

#### D6

```text
请列出 ctx://agent/analysis-agent/memories 下的所有记忆，并读取每条记忆的内容。
```

Expected:

- only 1 memory is shown
- the analyst’s A/B test result from `D5`
- the supplier note from `query-agent` does not appear

#### D7

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

Expected:

- the promo rule promoted in `D2` is visible

#### D8

```text
请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。
```

Expected:

- Claude chooses `contexthub_store`
- Claude chooses `contexthub_promote`
- a second shared memory is created

#### D9

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
```

Expected:

- 2 shared memories are shown
- the promo rule from `D2`
- the push-time recommendation from `D8`

### Phase 3: return to `query-agent`

Go back to Terminal 4 for `D10`.

#### D10

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容，并读取每条记忆的内容。
```

Expected:

- `query-agent` can see both shared items
- the supplier negotiation note from `D3` still does not appear in shared memory

## 6. Demo checkpoints

If the run is healthy, the manual evidence should look like this:

- `D1`: `contexthub_store`
- `D2`: `contexthub_promote`
- `D3`: `contexthub_store`
- `D4`: 2 private memories under `ctx://agent/query-agent/memories`
- `D5`: `contexthub_store`
- `D6`: 1 private memory under `ctx://agent/analysis-agent/memories`
- `D7`: 1 shared memory under `ctx://team/engineering/memories/shared_knowledge`
- `D8`: `contexthub_store` + `contexthub_promote`
- `D9`: 2 shared memories
- `D10`: the same 2 shared memories visible from `query-agent`

## 7. Troubleshooting

If Claude cannot use the MCP tools:

- confirm `/mcp` shows `contexthub` connected
- confirm `ContextHub` health endpoint is up
- confirm `ALL_PROXY` / `HTTP_PROXY` are blank in the MCP config env

If `ContextHub` fails to start:

```bash
cd "path to ContextHub"
docker compose up -d
python -m uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```
