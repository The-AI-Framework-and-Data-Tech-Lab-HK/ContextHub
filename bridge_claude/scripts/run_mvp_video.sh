#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

load_jiuwen_deepseek_env
set_claude_deepseek_env
require_claude_cli

export CONTEXTHUB_URL="${CONTEXTHUB_URL:-http://127.0.0.1:8000}"
export CONTEXTHUB_API_KEY="${CONTEXTHUB_API_KEY:-changeme}"
export CONTEXTHUB_ACCOUNT_ID="${CONTEXTHUB_ACCOUNT_ID:-acme}"
export PYTHONUNBUFFERED=1

python "${SCRIPT_DIR}/run_agent_prompt_steps.py"
