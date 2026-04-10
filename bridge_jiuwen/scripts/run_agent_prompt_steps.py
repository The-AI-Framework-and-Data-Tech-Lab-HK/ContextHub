from __future__ import annotations

import argparse
import asyncio
import ast
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from websockets.sync.client import connect

WS_URL = os.getenv("JIUWEN_WS_URL", "ws://127.0.0.1:19001/ws")
APP_WS_URL = os.getenv("JIUWEN_APP_WS_URL", "ws://127.0.0.1:19000/ws")
CONTEXTHUB_URL = os.getenv("CONTEXTHUB_URL", "http://127.0.0.1:8000").rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://contexthub:contexthub@localhost:5432/contexthub")
ACCOUNT_ID = os.getenv("CONTEXTHUB_ACCOUNT_ID", "acme")
AGENT_ID = os.getenv("CONTEXTHUB_AGENT_ID", "jiuwenclaw")
ENGINEERING_TEAM_ID = os.getenv("ENGINEERING_TEAM_ID", "00000000-0000-0000-0000-000000000002")
TEAM_WRITERS = tuple(dict.fromkeys((AGENT_ID, "query-agent", "analysis-agent")))
CHAT_MODE = os.getenv("JIUWEN_CHAT_MODE", "agent").strip().lower() or "agent"


@dataclass
class StepResult:
    """Minimal turn output: the prompt, tool traces, and final answer."""

    prompt: str
    tool_results: list[dict[str, Any]]
    final_text: str


@dataclass
class StepDef:
    """One prompt from the 10-step MVP story."""

    name: str
    prompt: str


@dataclass
class PhaseDef:
    """A group of steps that should run under one Jiuwen agent identity."""

    name: str
    expected_agent: str
    steps: list[StepDef]


def clear_proxy_env() -> None:
    # Local Jiuwen endpoints should not go through shell proxy settings.
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def port_open(host: str, port: int) -> bool:
    # Quick TCP liveness check for local services.
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def check_http(url: str) -> tuple[bool, str]:
    # Verify an HTTP endpoint is reachable and return its body for debugging.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
        return True, body
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def ensure_services() -> None:
    # Fail early with a short message naming the missing service.
    ok, detail = check_http(f"{CONTEXTHUB_URL}/health")
    if not ok:
        raise RuntimeError(f"ContextHub is down: {detail}")

    app = urlparse(APP_WS_URL)
    if not port_open(app.hostname or "127.0.0.1", app.port or 80):
        raise RuntimeError(f"jiuwenclaw-app is down: {APP_WS_URL}")

    web = urlparse(WS_URL)
    if not port_open(web.hostname or "127.0.0.1", web.port or 80):
        raise RuntimeError(f"jiuwenclaw-web is down: {WS_URL}")


async def _ensure_team_membership() -> None:
    # Let the common demo agent identities promote to engineering during the demo.
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("SELECT set_config('app.account_id', $1, false)", ACCOUNT_ID)
        for agent_id in TEAM_WRITERS:
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


def ensure_team_membership() -> None:
    # Keep the simple runner usable without manual DB setup.
    asyncio.run(_ensure_team_membership())


async def _clear_demo_memories() -> None:
    # Start each guided run from a clean slate for both agent spaces and the shared shelf.
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("SELECT set_config('app.account_id', $1, false)", ACCOUNT_ID)

        prefixes = [
            "ctx://agent/query-agent/memories/%",
            "ctx://agent/analysis-agent/memories/%",
            "ctx://team/engineering/memories/shared_knowledge/%",
        ]
        if AGENT_ID not in {"query-agent", "analysis-agent"}:
            prefixes.append(f"ctx://agent/{AGENT_ID}/memories/%")

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


def clear_demo_memories() -> None:
    # Reset demo memory state so every run starts clean.
    asyncio.run(_clear_demo_memories())


async def _clear_auto_capture_memories() -> None:
    # Auto-capture and URI bookkeeping memories make D4/D6 drift from the video plan.
    import asyncpg

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
                    tags @> ARRAY['auto-capture']::text[]
                 OR coalesce(l0_content, '') LIKE '%ctx://agent/%'
                 OR coalesce(l0_content, '') LIKE '%ctx://team/%'
                 OR coalesce(l1_content, '') LIKE '%ctx://agent/%'
                 OR coalesce(l1_content, '') LIKE '%ctx://team/%'
               )
            """
        )
    finally:
        await conn.close()


def clear_auto_capture_memories() -> None:
    # Keep the prompt demo focused on intentional stores/promotes only.
    asyncio.run(_clear_auto_capture_memories())


def request(ws: Any, request_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    # Send one Jiuwen RPC request and wait for the matching response.
    ws.send(json.dumps({"type": "req", "id": request_id, "method": method, "params": params}, ensure_ascii=False))
    while True:
        frame = json.loads(ws.recv(timeout=30))
        if frame.get("type") == "res" and frame.get("id") == request_id:
            return frame


def run_step(ws: Any, session_id: str, prompt: str) -> StepResult:
    # Send one prompt and collect tool results plus the final assistant reply.
    request(
        ws,
        f"req_{int(time.time() * 1000)}",
        "chat.send",
        {"session_id": session_id, "content": prompt, "mode": CHAT_MODE},
    )

    tool_results: list[dict[str, Any]] = []
    final_text = ""

    while True:
        frame = json.loads(ws.recv(timeout=120))
        if frame.get("type") != "event":
            continue
        event = frame.get("event")
        payload = frame.get("payload", {})
        if event == "chat.tool_result":
            tool_results.append(
                {
                    "tool_name": str(payload.get("tool_name") or "").strip(),
                    "result": summarize_tool_result(
                        str(payload.get("tool_name") or "").strip(),
                        payload.get("result"),
                    ),
                }
            )
        elif event == "chat.error":
            final_text = str(payload.get("error") or payload.get("message") or "").strip()
            return StepResult(prompt=prompt, tool_results=tool_results, final_text=final_text)
        elif event == "chat.final":
            final_text = str(payload.get("content") or "").strip()
        elif event == "chat.processing_status" and not payload.get("is_processing", True):
            return StepResult(prompt=prompt, tool_results=tool_results, final_text=final_text)


def parse_payload(raw: Any) -> Any:
    # Jiuwen tool results may arrive as dicts, JSON strings, or python-literal strings.
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw

    try:
        parsed: Any = json.loads(raw)
    except Exception:  # noqa: BLE001
        try:
            parsed = ast.literal_eval(raw)
        except Exception:  # noqa: BLE001
            return raw

    while isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
            continue
        except Exception:  # noqa: BLE001
            try:
                parsed = ast.literal_eval(parsed)
                continue
            except Exception:  # noqa: BLE001
                break
    return parsed


def normalize_result(raw: Any) -> Any:
    # Convert raw tool payloads into the simplest useful shape for terminal viewing.
    parsed = parse_payload(raw)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return parsed
    return shorten_text(str(parsed))


def shorten_text(value: str, limit: int = 120) -> str:
    # Keep previews readable in terminal output.
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def summarize_tool_result(tool_name: str, raw: Any) -> Any:
    # Show only the proof fields we care about for each tool.
    parsed = normalize_result(raw)
    if isinstance(parsed, dict):
        if "error" in parsed:
            return {"error": parsed["error"]}
        if tool_name in {"contexthub_store", "contexthub_promote"}:
            return {
                "uri": parsed.get("uri"),
                "scope": parsed.get("scope"),
                "owner_space": parsed.get("owner_space"),
            }
        if tool_name == "ls":
            items = parsed.get("items")
            if isinstance(items, list):
                return {"count": len(items), "items": items[:3]}
        if tool_name == "read":
            return {
                "uri": parsed.get("uri"),
                "level": parsed.get("level"),
                "preview": shorten_text(str(parsed.get("content") or "")),
            }
        return parsed
    if isinstance(parsed, list):
        return {"count": len(parsed), "items": parsed[:3]}
    return shorten_text(str(parsed))


def print_step(name: str, result: StepResult) -> None:
    # Print the prompt, tool trace, and answer with no extra assertion noise.
    print(f"\n{name}")
    print(f"prompt: {result.prompt}")
    print(f"tool_results: {json.dumps(result.tool_results, ensure_ascii=False)}")
    print(f"result: {result.final_text}")


def inferred_owner_space(result: StepResult) -> str | None:
    # Infer the live Jiuwen agent identity from any agent-scoped tool output in the step.
    for item in result.tool_results:
        payload = item.get("result")
        if isinstance(payload, dict) and payload.get("scope") == "agent" and payload.get("owner_space"):
            return str(payload["owner_space"])
        if (
            isinstance(payload, dict)
            and payload.get("scope") == "agent"
            and isinstance(payload.get("uri"), str)
        ):
            uri = payload["uri"]
            if uri.startswith("ctx://agent/"):
                parts = uri.split("/")
                if len(parts) >= 4:
                    return parts[3]
    return None


def run_phase(phase: PhaseDef) -> None:
    # Open one Jiuwen session for a phase and print each step in order.
    session_id = f"sess_prompt_steps_{int(time.time())}"
    seen_agent: str | None = None
    with connect(WS_URL, open_timeout=10, close_timeout=5) as ws:
        ack = json.loads(ws.recv(timeout=10))
        if ack.get("event") != "connection.ack":
            raise RuntimeError(f"unexpected first frame: {ack}")

        created = request(ws, "session-create", "session.create", {"session_id": session_id})
        if not created.get("ok"):
            raise RuntimeError(f"session.create failed: {created}")

        for step in phase.steps:
            result = run_step(ws, session_id, step.prompt)
            print_step(step.name, result)
            seen_agent = seen_agent or inferred_owner_space(result)
            clear_auto_capture_memories()

    if seen_agent and seen_agent != phase.expected_agent:
        raise RuntimeError(
            f"Phase {phase.name} expected agent '{phase.expected_agent}', "
            f"but Jiuwen appears to be running as '{seen_agent}'."
        )


def parse_args() -> argparse.Namespace:
    # Allow the operator to run one phase at a time during manual agent switching.
    parser = argparse.ArgumentParser(description="Run the Jiuwen prompt demo in simple prompt/result/tool-result form.")
    parser.add_argument(
        "--phase",
        choices=("query", "analysis", "query-return", "all"),
        default="all",
        help="Which phase of the 10-step MVP to run.",
    )
    return parser.parse_args()


def main() -> None:
    # Follow the 10-step MVP plan, but let the operator control agent switching phase by phase.
    args = parse_args()
    clear_proxy_env()
    ensure_services()
    if args.phase == "query":
        clear_demo_memories()
    else:
        clear_auto_capture_memories()
    ensure_team_membership()

    phases = {
        "query": PhaseDef(
            name="query",
            expected_agent="query-agent",
            steps=[
                StepDef(
                    "D1",
                    "请记住：春季促销活动规则，满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间是4月1日到15日。",
                ),
                StepDef(
                    "D2",
                    "请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。",
                ),
                StepDef(
                    "D3",
                    "请再记住一条：供应商谈判备忘，春季促销的供货底价不能低于零售价的60%，这条只留在我的私有空间，不要共享。",
                ),
                StepDef(
                    "D4",
                    "请列出我的私有空间的所有记忆。",
                ),
            ],
        ),
        "analysis": PhaseDef(
            name="analysis",
            expected_agent="analysis-agent",
            steps=[
                StepDef(
                    "D5",
                    "请记住：上季度 A/B 测试初步结果——B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。",
                ),
                StepDef(
                    "D6",
                    "请列出我的私有空间的所有记忆。",
                ),
                StepDef(
                    "D7",
                    "请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。",
                ),
                StepDef(
                    "D8",
                    "请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。",
                ),
                StepDef(
                    "D9",
                    "请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。",
                ),
            ],
        ),
        "query-return": PhaseDef(
            name="query-return",
            expected_agent="query-agent",
            steps=[
                StepDef(
                    "D10",
                    "请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。",
                ),
            ],
        ),
    }

    if args.phase == "all":
        raise RuntimeError(
            "请分阶段运行这个脚本：先用 --phase query，再切换到 analysis-agent 用 --phase analysis，"
            "最后切回 query-agent 用 --phase query-return。"
        )

    print(f"\nPhase: {args.phase} (expected agent: {phases[args.phase].expected_agent})")
    run_phase(phases[args.phase])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
