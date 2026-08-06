#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

load_jiuwen_deepseek_env

export DEEPSEEK_API_BASE
export DEEPSEEK_API_KEY
export DEEPSEEK_MODEL_NAME
export DEEPSEEK_MODEL_PROVIDER
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-local-deepseek-gateway}"

python "${CONTEXT_HUB_DIR}/bridge_claude/src/gateway.py"

