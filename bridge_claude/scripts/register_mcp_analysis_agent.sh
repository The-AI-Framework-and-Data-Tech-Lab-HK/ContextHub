#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

require_claude_cli
load_jiuwen_deepseek_env
set_claude_deepseek_env

claude mcp add --transport stdio --scope project contexthub \
  --env CONTEXTHUB_URL="${CONTEXTHUB_URL:-http://127.0.0.1:8000}" \
  --env CONTEXTHUB_API_KEY="${CONTEXTHUB_API_KEY:-changeme}" \
  --env CONTEXTHUB_ACCOUNT_ID="${CONTEXTHUB_ACCOUNT_ID:-acme}" \
  --env CONTEXTHUB_AGENT_ID="analysis-agent" \
  -- python "${CONTEXT_HUB_DIR}/bridge_claude/src/server.py"

echo "Registered project-scoped MCP server 'contexthub' for analysis-agent."
warn_direct_deepseek_support
