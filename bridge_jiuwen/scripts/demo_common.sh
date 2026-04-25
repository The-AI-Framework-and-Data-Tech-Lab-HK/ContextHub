#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
PROMPT_STEPS_SCRIPT="${REPO_ROOT}/ContextHub/bridge_jiuwen/scripts/run_agent_prompt_steps.py"
CONTEXTHUB_HEALTH_URL="${CONTEXTHUB_HEALTH_URL:-http://127.0.0.1:8000/health}"
JIUWEN_WEB_URL="${JIUWEN_WEB_URL:-http://127.0.0.1:19001/}"
APP_SERVICE="${APP_SERVICE:-jiuwenclaw-app}"
WEB_SERVICE="${WEB_SERVICE:-jiuwenclaw-web}"

clear_proxy_env() {
  unset ALL_PROXY all_proxy HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
  export NO_PROXY="127.0.0.1,localhost"
}

wait_http_ok() {
  local url="$1"
  local name="$2"
  local retries="${3:-30}"
  local delay="${4:-1}"

  for ((i=1; i<=retries; i++)); do
    if curl --noproxy '*' -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done

  echo "${name} is down: ${url}" >&2
  return 1
}

wait_tcp_ok() {
  local host="$1"
  local port="$2"
  local name="$3"
  local retries="${4:-30}"
  local delay="${5:-1}"

  for ((i=1; i<=retries; i++)); do
    if python - <<PY >/dev/null 2>&1
import socket
sock = socket.create_connection(("${host}", ${port}), timeout=2)
sock.close()
PY
    then
      return 0
    fi
    sleep "$delay"
  done

  echo "${name} is down: ${host}:${port}" >&2
  return 1
}

require_services() {
  wait_http_ok "$CONTEXTHUB_HEALTH_URL" "ContextHub"
  wait_tcp_ok "127.0.0.1" "19000" "jiuwenclaw-app"
  wait_http_ok "$JIUWEN_WEB_URL" "jiuwenclaw-web"
}

switch_agent() {
  local agent_id="$1"
  echo "==> Switching Jiuwen to agent: ${agent_id}"
  systemctl --user set-environment CONTEXTHUB_AGENT_ID="${agent_id}"
  systemctl --user restart "${APP_SERVICE}" "${WEB_SERVICE}"
  wait_tcp_ok "127.0.0.1" "19000" "jiuwenclaw-app"
  wait_http_ok "$JIUWEN_WEB_URL" "jiuwenclaw-web"
}

run_phase() {
  local phase="$1"
  echo "==> Running phase: ${phase}"
  clear_proxy_env
  "${PYTHON_BIN}" -u "${PROMPT_STEPS_SCRIPT}" --phase "${phase}"
}
