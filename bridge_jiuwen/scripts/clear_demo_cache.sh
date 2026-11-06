#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/demo_common.sh"

clear_proxy_env
require_services

echo "==> Clearing demo memories"
"${PYTHON_BIN}" - <<'PY'
import asyncio
import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://contexthub:contexthub@localhost:5432/contexthub")
ACCOUNT_ID = os.getenv("CONTEXTHUB_ACCOUNT_ID", "acme")

async def main() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("SELECT set_config('app.account_id', $1, false)", ACCOUNT_ID)
        await conn.execute(
            """
            UPDATE contexts
               SET status = 'deleted',
                   deleted_at = NOW(),
                   updated_at = NOW(),
                   version = version + 1
             WHERE status != 'deleted'
               AND (
                    uri LIKE 'ctx://agent/query-agent/memories/%'
                 OR uri LIKE 'ctx://agent/analysis-agent/memories/%'
                 OR uri LIKE 'ctx://agent/jiuwenclaw/memories/%'
                 OR uri LIKE 'ctx://team/engineering/memories/shared_knowledge/%'
               )
            """
        )
    finally:
        await conn.close()

asyncio.run(main())
PY

echo "Demo cache cleared."
