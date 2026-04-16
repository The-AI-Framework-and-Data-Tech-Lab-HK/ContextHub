#!/usr/bin/env bash
set -euo pipefail

# Start required local services (PostgreSQL + Neo4j) then run AMC API.
# Usage:
#   bash scripts/start_amc.sh
# Optional env:
#   AMC_HOST=127.0.0.1 AMC_PORT=8000 AMC_RELOAD=1 bash scripts/start_amc.sh
#   AMC_INSTALL_DEPS=0 bash scripts/start_amc.sh   # skip pip bootstrap

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AMC_HOST="${AMC_HOST:-127.0.0.1}"
AMC_PORT="${AMC_PORT:-8000}"
AMC_RELOAD="${AMC_RELOAD:-1}"
AMC_INSTALL_DEPS="${AMC_INSTALL_DEPS:-1}"
PYTHON_BIN="python"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

ensure_python_env() {
  if [[ ! -x ".venv/bin/python" ]]; then
    echo "[AMC] creating virtualenv at .venv ..."
    "$PYTHON_BIN" -m venv .venv
  fi

  # Keep behavior aligned with README setup steps.
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
  python -m pip install -U pip

  if [[ "$AMC_INSTALL_DEPS" != "1" ]]; then
    echo "[AMC] skip dependency bootstrap (AMC_INSTALL_DEPS=0)"
    return 0
  fi

  if ! python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo "[AMC] installing project dependencies into .venv ..."
    python -m pip install -e ".[dev]"
  fi
}

check_port_open() {
  local port="$1"
  "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.6)
try:
    s.connect(("127.0.0.1", port))
    ok = True
except Exception:
    ok = False
finally:
    s.close()
print("1" if ok else "0")
PY
}

start_service_if_needed() {
  local service_name="$1"
  local port="$2"

  if [[ "$(check_port_open "$port")" == "1" ]]; then
    echo "[AMC] ${service_name} already listening on :${port}"
    return 0
  fi

  echo "[AMC] starting ${service_name} ..."
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start "$service_name"
  else
    sudo service "$service_name" start
  fi

  if [[ "$(check_port_open "$port")" != "1" ]]; then
    echo "[AMC] ERROR: ${service_name} is still not listening on :${port}" >&2
    exit 1
  fi
  echo "[AMC] ${service_name} is ready on :${port}"
}

ensure_python_env
PYTHON_BIN=".venv/bin/python"
start_service_if_needed "postgresql" "5432"
start_service_if_needed "neo4j" "7687"

UVICORN_BIN=".venv/bin/uvicorn"

if [[ "$AMC_RELOAD" == "1" ]]; then
  exec "$UVICORN_BIN" main:app --app-dir src --host "$AMC_HOST" --port "$AMC_PORT" --reload
else
  exec "$UVICORN_BIN" main:app --app-dir src --host "$AMC_HOST" --port "$AMC_PORT"
fi

