#!/usr/bin/env bash
set -euo pipefail

# AMC v0 sidecar smoke test via curl.
# Verifies:
# 1) /health is reachable
# 2) /ingest returns {"ingested": true}
# 3) /assemble returns empty context
# 4) openclaw_message/<session_id>/ gets at least one file

SIDECAR_URL="${SIDECAR_URL:-http://127.0.0.1:9200}"
SESSION_ID="${SESSION_ID:-smoke-session-001}"
OUTPUT_DIR="${OUTPUT_DIR:-/home/qchenax/ContextHub/openclaw_message}"

echo "[amc-v0-smoke] sidecar_url=${SIDECAR_URL}"
echo "[amc-v0-smoke] session_id=${SESSION_ID}"
echo "[amc-v0-smoke] output_dir=${OUTPUT_DIR}"

TARGET_DIR="${OUTPUT_DIR}/${SESSION_ID}"
mkdir -p "${TARGET_DIR}"

before_count=$(python - <<'PY' "${TARGET_DIR}"
import os, sys
path = sys.argv[1]
count = 0
if os.path.isdir(path):
    count = sum(1 for x in os.scandir(path) if x.is_file())
print(count)
PY
)

echo "[amc-v0-smoke] files_before=${before_count}"

health_json=$(curl -sS "${SIDECAR_URL}/health")
python - <<'PY' "${health_json}"
import json, sys
payload = json.loads(sys.argv[1])
assert payload.get("status") == "ok", f"unexpected /health payload: {payload}"
print("[amc-v0-smoke] /health ok")
PY

ingest_json=$(curl -sS -X POST "${SIDECAR_URL}/ingest" \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: agent-smoke" \
  -H "X-Account-Id: acc-smoke" \
  -d "$(cat <<EOF
{
  "sessionId": "${SESSION_ID}",
  "sessionKey": "session:smoke",
  "isHeartbeat": false,
  "message": {
    "role": "user",
    "content": "hello from smoke test"
  }
}
EOF
)")

python - <<'PY' "${ingest_json}"
import json, sys
payload = json.loads(sys.argv[1])
assert payload.get("ingested") is True, f"unexpected /ingest payload: {payload}"
print("[amc-v0-smoke] /ingest ok")
PY

assemble_json=$(curl -sS -X POST "${SIDECAR_URL}/assemble" \
  -H "Content-Type: application/json" \
  -d "$(cat <<EOF
{
  "sessionId": "${SESSION_ID}",
  "messages": [
    { "role": "user", "content": "test assemble" }
  ],
  "tokenBudget": 4096
}
EOF
)")

python - <<'PY' "${assemble_json}"
import json, sys
payload = json.loads(sys.argv[1])
assert payload.get("messages") == [], f"expected empty messages: {payload}"
assert int(payload.get("estimatedTokens", -1)) == 0, f"expected estimatedTokens=0: {payload}"
print("[amc-v0-smoke] /assemble ok")
PY

after_count=$(python - <<'PY' "${TARGET_DIR}"
import os, sys
path = sys.argv[1]
count = 0
if os.path.isdir(path):
    count = sum(1 for x in os.scandir(path) if x.is_file())
print(count)
PY
)

echo "[amc-v0-smoke] files_after=${after_count}"
if [ "${after_count}" -le "${before_count}" ]; then
  echo "[amc-v0-smoke] ERROR: no new ingest file written under ${TARGET_DIR}" >&2
  exit 1
fi

echo "[amc-v0-smoke] PASS"
