#!/usr/bin/env python
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import asyncpg


ROOT = Path(__file__).resolve().parents[2]
SERVER_PY = ROOT / "bridge_claude" / "src" / "server.py"
DEFAULT_DB = "postgresql://contexthub:contexthub@localhost:5432/contexthub"
DEFAULT_ACCOUNT = "acme"
ENGINEERING_TEAM_ID = "00000000-0000-0000-0000-000000000002"
ALLOWED_TOOLS = [
    "mcp__contexthub__contexthub_store",
    "mcp__contexthub__contexthub_promote",
    "mcp__contexthub__contexthub_skill_publish",
    "mcp__contexthub__ls",
    "mcp__contexthub__read",
    "mcp__contexthub__grep",
    "mcp__contexthub__stat",
]
STEP_TIMEOUT_S = 120


PROMPTS: list[tuple[str, str, str]] = [
    (
        "query-agent",
        "D1",
        "请记住：春季促销活动规则 —— 满 300 减 50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间 4 月 1 日至 15 日。",
    ),
    (
        "query-agent",
        "D2",
        "请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。",
    ),
    (
        "query-agent",
        "D3",
        "请再记住一条：供应商谈判备忘 —— 春季促销的供货底价不能低于零售价的 60%，这个底线不要对外透露。这条只留在我的私有空间，不要共享。",
    ),
    ("query-agent", "D4", "请列出 ctx://agent/query-agent/memories 下的所有记忆，并读取每条记忆的内容。"),
    (
        "analysis-agent",
        "D5",
        "请记住：上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。",
    ),
    ("analysis-agent", "D6", "请列出 ctx://agent/analysis-agent/memories 下的所有记忆，并读取每条记忆的内容。"),
    ("analysis-agent", "D7", "请列出 ctx://team/engineering/memories/shared_knowledge 下的内容"),
    (
        "analysis-agent",
        "D8",
        "请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。",
    ),
    ("analysis-agent", "D9", "请列出 ctx://team/engineering/memories/shared_knowledge 下的内容"),
    ("query-agent", "D10", "请列出 ctx://team/engineering/memories/shared_knowledge 下的内容，并读取每条记忆的内容。"),
]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def check_service(url: str, name: str) -> None:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=5) as response:
            if response.status >= 400:
                raise RuntimeError(f"{name} unhealthy: HTTP {response.status}")
    except URLError as exc:
        raise RuntimeError(f"{name} unavailable at {url}: {exc}") from exc


async def seed_team_memberships() -> None:
    database_url = os.getenv("DATABASE_URL", DEFAULT_DB)
    account_id = os.getenv("CONTEXTHUB_ACCOUNT_ID", DEFAULT_ACCOUNT)
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("SELECT set_config('app.account_id', $1, false)", account_id)
        for agent_id in ("query-agent", "analysis-agent"):
            await conn.execute(
                """
                INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
                VALUES ($1, $2, 'member', 'read_write', FALSE)
                ON CONFLICT DO NOTHING
                """,
                agent_id,
                ENGINEERING_TEAM_ID,
            )
    finally:
        await conn.close()


async def clear_demo_cache() -> None:
    database_url = os.getenv("DATABASE_URL", DEFAULT_DB)
    account_id = os.getenv("CONTEXTHUB_ACCOUNT_ID", DEFAULT_ACCOUNT)
    prefixes = [
        "ctx://agent/query-agent/memories/%",
        "ctx://agent/analysis-agent/memories/%",
        "ctx://team/engineering/memories/shared_knowledge/%",
    ]
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("SELECT set_config('app.account_id', $1, false)", account_id)
        for prefix in prefixes:
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


def build_mcp_config(agent_id: str) -> Path:
    config = {
        "mcpServers": {
            "contexthub": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(SERVER_PY)],
                "env": {
                    "CONTEXTHUB_URL": os.getenv("CONTEXTHUB_URL", "http://127.0.0.1:8000"),
                    "CONTEXTHUB_API_KEY": os.getenv("CONTEXTHUB_API_KEY", "changeme"),
                    "CONTEXTHUB_ACCOUNT_ID": os.getenv("CONTEXTHUB_ACCOUNT_ID", DEFAULT_ACCOUNT),
                    "CONTEXTHUB_AGENT_ID": agent_id,
                    "ALL_PROXY": "",
                    "all_proxy": "",
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                    "http_proxy": "",
                    "https_proxy": "",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                },
            }
        }
    }
    handle = tempfile.NamedTemporaryFile("w", suffix=f"-{agent_id}-mcp.json", delete=False)
    with handle:
        json.dump(config, handle)
    return Path(handle.name)


def summarize_tool_result(value: Any) -> Any:
    if isinstance(value, dict):
        keys = ("uri", "scope", "owner_space", "count", "items", "level", "preview")
        summary = {key: value[key] for key in keys if key in value}
        return summary or value
    return value


def run_step(agent_id: str, step_name: str, prompt: str) -> tuple[list[dict[str, Any]], str]:
    config_path = build_mcp_config(agent_id)
    try:
        command = [
            "claude",
            "--print",
            "--verbose",
            "--bare",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            ",".join(ALLOWED_TOOLS),
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(config_path),
            "--no-session-persistence",
        ]
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=STEP_TIMEOUT_S,
            check=False,
            env=os.environ.copy(),
        )
    finally:
        config_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            textwrap.dedent(
                f"""\
                {step_name} failed with exit code {result.returncode}
                STDERR:
                {result.stderr.strip()}
                STDOUT:
                {result.stdout.strip()}
                """
            ).strip()
        )

    tool_names: dict[str, str] = {}
    tool_results: list[dict[str, Any]] = []
    final_result = ""

    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        event = json.loads(raw_line)

        if event.get("type") == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "tool_use":
                    tool_names[block["id"]] = block["name"].removeprefix("mcp__contexthub__")
                if block.get("type") == "text":
                    final_result = block["text"]

        if event.get("type") == "user":
            message = event.get("message", {})
            for block in message.get("content", []):
                if block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                tool_name = tool_names.get(tool_use_id, tool_use_id or "unknown")
                payload = message.get("tool_use_result", {})
                structured = payload.get("structuredContent") if isinstance(payload, dict) else None
                if structured is None:
                    content = block.get("content")
                    try:
                        structured = json.loads(content)
                    except Exception:
                        structured = content
                tool_results.append(
                    {
                        "tool_name": tool_name,
                        "result": summarize_tool_result(structured),
                    }
                )

        if event.get("type") == "result":
            final_result = event.get("result", final_result)

    return tool_results, final_result


def main() -> None:
    require_env("ANTHROPIC_BASE_URL")
    require_env("ANTHROPIC_AUTH_TOKEN")
    require_env("ANTHROPIC_MODEL")
    check_service(os.getenv("CONTEXTHUB_URL", "http://127.0.0.1:8000") + "/health", "ContextHub")

    import asyncio

    asyncio.run(seed_team_memberships())
    asyncio.run(clear_demo_cache())
    print("Claude demo cache cleared.")

    for agent_id, step_name, prompt in PROMPTS:
        tool_results, final_result = run_step(agent_id, step_name, prompt)
        print()
        print(step_name)
        print(f"prompt: {prompt}")
        print(
            "tool_results: "
            + json.dumps(tool_results, ensure_ascii=False, default=str)
        )
        print(f"result: {final_result}")

    print("Claude prompt-step demo completed.")


if __name__ == "__main__":
    main()
