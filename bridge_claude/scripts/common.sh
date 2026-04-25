#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_CLAUDE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONTEXT_HUB_DIR="$(cd "${BRIDGE_CLAUDE_DIR}/.." && pwd)"
JIUWEN_ENV_FILE="${JIUWEN_ENV_FILE:-${HOME}/.jiuwenclaw/config/.env}"

read_jiuwen_env_var() {
  local name="$1"
  python - "$JIUWEN_ENV_FILE" "$name" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
target = sys.argv[2]
if not env_path.exists():
    raise SystemExit(1)

for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.strip().rstrip("\r")
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if key != target:
        continue
    value = value.strip().strip('"').strip("'")
    print(value)
    break
PY
}

load_jiuwen_deepseek_env() {
  if [[ ! -f "${JIUWEN_ENV_FILE}" ]]; then
    echo "Jiuwen env file not found: ${JIUWEN_ENV_FILE}" >&2
    return 1
  fi

  export DEEPSEEK_API_BASE="$(read_jiuwen_env_var API_BASE || true)"
  export DEEPSEEK_API_KEY="$(read_jiuwen_env_var API_KEY || true)"
  export DEEPSEEK_MODEL_NAME="$(read_jiuwen_env_var MODEL_NAME || true)"
  export DEEPSEEK_MODEL_PROVIDER="$(read_jiuwen_env_var MODEL_PROVIDER || true)"

  if [[ -z "${DEEPSEEK_API_BASE:-}" || -z "${DEEPSEEK_API_KEY:-}" || -z "${DEEPSEEK_MODEL_NAME:-}" ]]; then
    echo "Missing DeepSeek config in ${JIUWEN_ENV_FILE}" >&2
    return 1
  fi
}

print_deepseek_summary() {
  echo "DeepSeek base: ${DEEPSEEK_API_BASE}"
  echo "DeepSeek model: ${DEEPSEEK_MODEL_NAME}"
  echo "DeepSeek provider: ${DEEPSEEK_MODEL_PROVIDER:-}"
  echo "DeepSeek key: present"
}

set_claude_deepseek_env() {
  export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-${DEEPSEEK_API_BASE%/}/anthropic}"
  export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-${DEEPSEEK_API_KEY}}"
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-${DEEPSEEK_MODEL_NAME}}"
  export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-${DEEPSEEK_MODEL_NAME}}"
  export ANTHROPIC_CUSTOM_MODEL_OPTION="${ANTHROPIC_CUSTOM_MODEL_OPTION:-${DEEPSEEK_MODEL_NAME}}"
  export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="${ANTHROPIC_CUSTOM_MODEL_OPTION_NAME:-DeepSeek Chat}"
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
  export API_TIMEOUT_MS="${API_TIMEOUT_MS:-600000}"
}

require_claude_cli() {
  if ! command -v claude >/dev/null 2>&1; then
    echo "Claude Code CLI is not installed or not on PATH." >&2
    return 1
  fi
}

warn_direct_deepseek_support() {
  cat <<'EOF'
Using DeepSeek's Anthropic-compatible endpoint for Claude Code:
- ANTHROPIC_BASE_URL=<deepseek base>/anthropic
- ANTHROPIC_AUTH_TOKEN=<deepseek key>
- ANTHROPIC_MODEL=<deepseek model>
- ANTHROPIC_DEFAULT_HAIKU_MODEL=<deepseek model>
EOF
}
