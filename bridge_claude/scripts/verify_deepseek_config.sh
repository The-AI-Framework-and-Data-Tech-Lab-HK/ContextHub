#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

load_jiuwen_deepseek_env
print_deepseek_summary
set_claude_deepseek_env
warn_direct_deepseek_support

python - <<'PY'
import json
import os
import sys

import httpx

base = os.environ["DEEPSEEK_API_BASE"].rstrip("/")
key = os.environ["DEEPSEEK_API_KEY"]
model = os.environ["DEEPSEEK_MODEL_NAME"]

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ],
    "stream": False,
}

resp = httpx.post(
    f"{base}/chat/completions",
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    },
    json=payload,
    timeout=30,
)
print(f"HTTP {resp.status_code}")
if resp.is_success:
    data = resp.json()
    msg = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
    print("DeepSeek reachable.")
    print("Preview:", msg[:200].replace("\n", " "))
else:
    print(resp.text[:500])
    sys.exit(1)
PY
