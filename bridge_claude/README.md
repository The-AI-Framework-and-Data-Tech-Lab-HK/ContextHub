# ContextHub Claude Code Bridge

Claude Code integration for ContextHub using MCP.

This bridge exposes the same seven MVP ContextHub tools used by the Jiuwen and
OpenClaw demos, but through a local MCP server that Claude Code can connect to:

- `ls`
- `read`
- `grep`
- `stat`
- `contexthub_store`
- `contexthub_promote`
- `contexthub_skill_publish`

## Files

- `src/bootstrap.py`: repo path bootstrap helper
- `src/server.py`: FastMCP server exposing ContextHub tools to Claude Code
- `.mcp.json.example`: example project-scoped Claude Code MCP config
- `scripts/clear_demo_cache.sh`: reset demo memories before a fresh run
- `scripts/start_query_agent.sh`: launch Claude Code as `query-agent`
- `scripts/start_analysis_agent.sh`: launch Claude Code as `analysis-agent`

## Quick start

1. Start ContextHub:

```bash
cd "path to ContextHub"
python -m uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

2. Load DeepSeek-backed config from the existing Jiuwen config and verify it:

```bash
cd "path to ContextHub"
bridge_claude/scripts/verify_deepseek_config.sh
```

This reads `API_BASE`, `API_KEY`, `MODEL_NAME`, and `MODEL_PROVIDER` from
`~/.jiuwenclaw/config/.env`.

This script also prepares the Claude Code env vars for DeepSeek's Anthropic-
compatible endpoint:

- `ANTHROPIC_BASE_URL=<API_BASE>/anthropic`
- `ANTHROPIC_AUTH_TOKEN=<API_KEY>`
- `ANTHROPIC_MODEL=<MODEL_NAME>`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL=<MODEL_NAME>`

3. Register the MCP server with Claude Code:

```bash
cd "path to ContextHub"
bridge_claude/scripts/register_mcp_query_agent.sh
```

4. Check `/mcp` inside Claude Code and verify the `contexthub` server is
connected.

5. Start Claude Code sessions with the two launch helpers:

```bash
bridge_claude/scripts/start_query_agent.sh
bridge_claude/scripts/start_analysis_agent.sh
```

These scripts:

- set `CONTEXTHUB_AGENT_ID` to the chosen identity
- load the existing DeepSeek config from `~/.jiuwenclaw/config/.env`
- forward those values into the shell environment for Claude Code

These launch scripts point Claude Code at DeepSeek directly using the
Anthropic-compatible endpoint:

- `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`
- `ANTHROPIC_AUTH_TOKEN=<your DeepSeek key>`
- `ANTHROPIC_MODEL=deepseek-chat`
