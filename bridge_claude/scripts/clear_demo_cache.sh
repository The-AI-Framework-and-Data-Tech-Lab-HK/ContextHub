#!/usr/bin/env bash

set -euo pipefail

SELF_PID="$$"
PARENT_PID="${PPID:-}"

kill_matching_processes() {
  local pattern="$1"
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    [[ "$pid" == "$SELF_PID" ]] && continue
    [[ -n "$PARENT_PID" && "$pid" == "$PARENT_PID" ]] && continue
    kill "$pid" >/dev/null 2>&1 || true
  done < <(pgrep -f "$pattern" || true)
}

kill_matching_processes 'bridge_claude/scripts/run_agent_prompt_steps.py'
kill_matching_processes 'claude --print --verbose'

# Give the old worker processes a moment to exit before clearing the database.
sleep 1

python - <<'PY'
import asyncio
import os

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://contexthub:contexthub@localhost:5432/contexthub")
ACCOUNT_ID = os.getenv("CONTEXTHUB_ACCOUNT_ID", "acme")

PREFIXES = [
    "ctx://agent/query-agent/memories/%",
    "ctx://agent/analysis-agent/memories/%",
    "ctx://agent/claude-agent/memories/%",
    "ctx://team/engineering/memories/shared_knowledge/%",
]


async def main() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("SELECT set_config('app.account_id', $1, false)", ACCOUNT_ID)
        for prefix in PREFIXES:
            await conn.execute(
                """
                UPDATE contexts
                   SET status = 'deleted',
                       deleted_at = NOW(),
                       updated_at = NOW(),
                       version = version + 1
                 WHERE uri LIKE $1
                   AND status != 'deleted'
                """,
                prefix,
            )
    finally:
        await conn.close()


asyncio.run(main())
print("Claude demo cache cleared and stale Claude demo processes stopped.")
PY
