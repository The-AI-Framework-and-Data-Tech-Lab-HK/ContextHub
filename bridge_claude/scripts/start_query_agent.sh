#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

require_claude_cli
load_jiuwen_deepseek_env
set_claude_deepseek_env

export CONTEXTHUB_AGENT_ID=query-agent
export DEEPSEEK_API_BASE
export DEEPSEEK_API_KEY
export DEEPSEEK_MODEL_NAME
export DEEPSEEK_MODEL_PROVIDER

exec claude "$@"
