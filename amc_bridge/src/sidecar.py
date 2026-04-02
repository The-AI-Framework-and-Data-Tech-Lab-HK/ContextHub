"""AMC v0 sidecar for OpenClaw context-engine bridge.

v0 behavior:
- ingest: write full incoming message payload to local files under openclaw_message/
- assemble: return empty context
- compact: delegated by TS bridge to OpenClaw runtime (not handled here)
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request

app = FastAPI(title="AMC v0 Sidecar")

_output_dir = Path("/home/qchenax/ContextHub/openclaw_message")
_seq = count(1)
_traj_seq = count(1)
_assemble_seq = count(1)
_session_buffers: dict[str, list[dict[str, Any]]] = {}
_trajectory_subdir = "trajectories"
_amc_commit_url = "http://127.0.0.1:8000/api/v1/amc/commit"
_amc_commit_timeout_s = 300.0
_amc_retrieve_url = "http://127.0.0.1:8000/api/v1/amc/retrieve"
_amc_retrieve_timeout_s = 20.0
_default_account_id = "openclaw"
_default_agent_id = "openclaw-sidecar"


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "").strip().lower()
    return ""


def _message_stop_reason(message: Any) -> str:
    if isinstance(message, dict):
        # Keep compatibility with possible naming variants.
        return str(
            message.get("stopReason")
            or message.get("stop_reason")
            or message.get("stopreason")
            or ""
        ).strip().lower()
    return ""


def _is_heartbeat_ingest(body: dict[str, Any], message: Any) -> bool:
    """
    Detect heartbeat events from OpenClaw ingest payload.
    """
    if bool(body.get("isHeartbeat")):
        return True
    if isinstance(message, dict) and bool(message.get("isHeartbeat")):
        return True
    text = _extract_text_from_message(message)
    if "heartbeat.md" in text.lower():
        return True
    return False


def _is_heartbeat_trajectory(records: list[dict[str, Any]]) -> bool:
    for rec in records:
        msg = rec.get("message")
        text = _extract_text_from_message(msg).lower()
        if "heartbeat.md" in text or "heartbeat_ok" in text:
            return True
        if isinstance(msg, dict) and bool(msg.get("isHeartbeat")):
            return True
    return False


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _extract_text_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return _extract_text_from_content(message.get("content"))


def _strip_query_timestamp_prefix(query: str) -> str:
    # Example: "[Wed 2026-04-01 17:40 GMT+8] Count the number ..."
    text = (query or "").strip()
    return re.sub(r"^\[[^\]]+\]\s*", "", text).strip()


def _latest_user_query_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if _message_role(msg) != "user":
            continue
        text = _extract_text_from_message(msg)
        if text:
            return _strip_query_timestamp_prefix(text)
    return ""


def _extract_query(records: list[dict[str, Any]]) -> str:
    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        if _message_role(msg) != "user":
            continue
        query = _extract_text_from_content(msg.get("content"))
        if query:
            return _strip_query_timestamp_prefix(query)
    return ""


def _format_tool_call_action(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "").strip()
    args = block.get("arguments")
    if not name:
        return ""
    if not isinstance(args, dict) or not args:
        return f"{name}()"

    arg_parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            value = json.dumps(v, ensure_ascii=False)
        else:
            value = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        arg_parts.append(f"{k}={value}")
    return f"{name}({', '.join(arg_parts)})"


def _find_prev_thinking(blocks: list[dict[str, Any]], from_index: int) -> str:
    i = from_index - 1
    while i >= 0:
        b = blocks[i]
        if str(b.get("type") or "") == "thinking":
            thinking = b.get("thinking")
            if isinstance(thinking, str):
                return thinking
            return str(thinking or "")
        # Stop when we crossed into another finished action/result section.
        if str(b.get("type") or "") == "toolCall":
            break
        i -= 1
    return ""


def _collect_action_result_text(blocks: list[dict[str, Any]], from_index: int) -> str:
    parts: list[str] = []
    i = from_index
    while i < len(blocks):
        b = blocks[i]
        btype = str(b.get("type") or "")
        if btype == "toolCall":
            break
        if btype == "text":
            text = b.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
                # Keep the first concrete text as the action result block.
                break
        i += 1
    return "\n".join(parts).strip()


def _build_sample_like_trajectory(flattened_contents: list[Any]) -> list[dict[str, Any]]:
    blocks = [b for b in flattened_contents if isinstance(b, dict)]
    steps: list[dict[str, Any]] = []
    step_no = 1

    for idx, block in enumerate(blocks):
        if str(block.get("type") or "") != "toolCall":
            continue

        thinking = _find_prev_thinking(blocks, idx)
        action = _format_tool_call_action(block)
        action_result = _collect_action_result_text(blocks, idx + 1)

        steps.append(
            {
                "Step": step_no,
                "Thinking": thinking,
                "Action": action,
                "Action_result": "",
                "Response": "",
                "meta": {"role": "AIMessage"},
            }
        )
        step_no += 1

        steps.append(
            {
                "Step": step_no,
                "Thinking": "",
                "Action": "",
                "Action_result": action_result,
                "Response": "",
                "meta": {"role": "ToolMessage"},
            }
        )
        step_no += 1

    return steps


def _commit_trajectory_to_amc(snapshot: dict[str, Any]) -> tuple[bool, str]:
    """Commit merged trajectory to AMC API."""
    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        return False, "missing_meta"

    session_id = str(meta.get("session_id") or "").strip()
    account_id = str(meta.get("account_id") or "").strip()
    agent_id = str(meta.get("agent_id") or "").strip()
    trajectory = snapshot.get("trajectory")
    if not isinstance(trajectory, list):
        return False, "invalid_trajectory"

    req_body: dict[str, Any] = {
        "session_id": session_id or "unknown_session",
        "task_id": f"openclaw-{session_id or 'unknown'}",
        "trajectory": trajectory,
        "labels": {},
        "is_incremental": False,
        "visualize_graph_png": False,
    }
    req_data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Account-Id": account_id,
        "X-Agent-Id": agent_id,
    }

    def _post_once(timeout_s: float) -> tuple[bool, str]:
        req = urllib.request.Request(
            _amc_commit_url,
            data=req_data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                body_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            return False, f"http_error:{e.code}:{body_text[:300]}"
        except Exception as e:
            return False, f"request_failed:{type(e).__name__}:{e}"

        if status >= 400:
            return False, f"http_error:{status}:{body_text[:300]}"
        return True, "ok"

    # Retry once with a larger timeout for heavy trajectories.
    ok, msg = _post_once(_amc_commit_timeout_s)
    if ok:
        return True, msg
    if "TimeoutError" in msg or "timed out" in msg:
        retry_timeout = max(_amc_commit_timeout_s * 2, 600.0)
        return _post_once(retry_timeout)
    return False, msg


def _retrieve_top1_abstract_from_amc(*, account_id: str, agent_id: str, query: str) -> tuple[str, str]:
    req_body: dict[str, Any] = {
        "query": {
            "task_description": query,
            "partial_trajectory": None,
            "constraints": {"tool_whitelist": []},
        },
        "scope": [],
        "owner_space": [],
        "top_k": 1,
        "include_full_clean_graph": False,
    }
    req = urllib.request.Request(
        _amc_retrieve_url,
        data=json.dumps(req_body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Account-Id": account_id,
            "X-Agent-Id": agent_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_amc_retrieve_timeout_s) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            body_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return "", f"http_error:{e.code}:{err[:300]}"
    except Exception as e:
        return "", f"request_failed:{type(e).__name__}:{e}"

    if status >= 400:
        return "", f"http_error:{status}:{body_text[:300]}"

    try:
        data = json.loads(body_text)
    except Exception:
        return "", "invalid_json_response"
    if not isinstance(data, dict):
        return "", "invalid_response_type"
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return "", "ok:no_hits"
    top = items[0]
    if not isinstance(top, dict):
        return "", "ok:invalid_top_item"
    abstract = top.get("abstract")
    if not isinstance(abstract, str) or not abstract.strip():
        return "", "ok:no_abstract"
    return abstract.strip(), "ok"


def _write_assemble_log(payload: dict[str, Any]) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    seq = next(_assemble_seq)
    out_dir = _output_dir / "assemble"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{ts}_{seq:06d}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_trajectory_snapshot(
    *,
    session_id: str,
    account_id: str,
    agent_id: str,
    records: list[dict[str, Any]],
    finished_reason: str,
) -> dict[str, Any] | None:
    if not records:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    seq = next(_traj_seq)
    out_dir = _output_dir / _trajectory_subdir / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    flattened_contents: list[Any] = []
    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            flattened_contents.extend(content)
        elif content is not None:
            flattened_contents.append(content)

    # Heartbeat trajectory: do not persist merged trajectory.
    if _is_heartbeat_trajectory(records):
        return None

    # v0 rule: only persist merged trajectories that contain at least one toolCall.
    has_tool_call = any(
        isinstance(block, dict) and str(block.get("type") or "") == "toolCall"
        for block in flattened_contents
    )
    if not has_tool_call:
        return None

    query = _extract_query(records)
    # Drop heartbeat-only/non-user traces to avoid polluting trajectory pool.
    if not query:
        return None

    payload: dict[str, Any] = {
        "query": query,
        "trajectory": _build_sample_like_trajectory(flattened_contents),
        "meta": {
            "session_id": session_id,
            "account_id": account_id,
            "agent_id": agent_id,
            "source": "trajectory_merged",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "trajectory_started_at": records[0].get("received_at"),
            "trajectory_ended_at": records[-1].get("received_at"),
            "finished_reason": finished_reason,
            "message_count": len(records),
        },
    }
    snapshot_path = out_dir / f"{ts}_{seq:06d}.json"
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["_snapshot_file_path"] = str(snapshot_path)
    return payload


def _flush_session_buffer(
    *,
    session_id: str,
    account_id: str,
    agent_id: str,
    buffer: list[dict[str, Any]],
    finished_reason: str,
) -> dict[str, Any] | None:
    snapshot = _write_trajectory_snapshot(
        session_id=session_id,
        account_id=account_id,
        agent_id=agent_id,
        records=buffer,
        finished_reason=finished_reason,
    )
    _cleanup_single_message_files(buffer)
    buffer.clear()
    return snapshot


def _commit_snapshot_background(snapshot: dict[str, Any]) -> None:
    """
    Best-effort background commit so ingest response is not blocked by AMC latency.
    """
    ok, msg = _commit_trajectory_to_amc(snapshot)
    if not ok:
        print(f"[amc-sidecar] background commit failed: {msg}")


def _cleanup_single_message_files(records: list[dict[str, Any]]) -> None:
    for rec in records:
        path = rec.get("_single_file_path")
        if not isinstance(path, str) or not path:
            continue
        p = Path(path)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            # Best-effort cleanup; keep ingest flow resilient.
            pass


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest")
async def ingest(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    body = await request.json()
    session_id = str(body.get("sessionId") or "unknown_session")
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S.%fZ")
    seq = next(_seq)
    agent_id = str(request.headers.get("x-agent-id") or _default_agent_id).strip()
    account_id = str(request.headers.get("x-account-id") or _default_account_id).strip()

    payload: dict[str, Any] = {
        "session_id": session_id,
        "account_id": account_id,
        "agent_id": agent_id,
        "received_at": now.isoformat(),
        "source": "ingest",
        "message": body.get("message"),
    }

    target_dir = _output_dir / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{ts}_{seq:06d}.json"
    target_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["_single_file_path"] = str(target_file)

    # Aggregate single messages into trajectory-level snapshots.
    message = payload.get("message")
    role = _message_role(message)
    stop_reason = _message_stop_reason(message)
    buffer = _session_buffers.setdefault(session_id, [])

    queued_commit = False
    commit_message = "skipped:not_flushed"

    # If a new user message arrives before previous trajectory closed, flush stale buffer.
    if role == "user" and buffer:
        snapshot = _flush_session_buffer(
            session_id=session_id,
            account_id=account_id,
            agent_id=agent_id,
            buffer=buffer,
            finished_reason="new_user_start_without_stop",
        )
        if snapshot is not None:
            background_tasks.add_task(_commit_snapshot_background, snapshot)
            queued_commit = True
            commit_message = "queued:flushed_by_new_user"

    buffer.append(payload)
    if stop_reason == "stop":
        snapshot = _flush_session_buffer(
            session_id=session_id,
            account_id=account_id,
            agent_id=agent_id,
            buffer=buffer,
            finished_reason="stop",
        )
        if snapshot is not None:
            background_tasks.add_task(_commit_snapshot_background, snapshot)
            queued_commit = True
            commit_message = "queued:flushed_by_stop"

    return {
        "ingested": True,
        "queued_commit": queued_commit,
        "commit_message": commit_message,
    }


@app.post("/assemble")
async def assemble(request: Request) -> dict[str, Any]:
    _ = await request.json()
    return {
        "messages": [],
        "estimatedTokens": 0,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AMC v0 sidecar")
    parser.add_argument("--port", type=int, default=9200, help="HTTP port for sidecar")
    parser.add_argument(
        "--output-dir",
        default="/home/qchenax/ContextHub/openclaw_message",
        help="Directory to persist ingested OpenClaw messages",
    )
    parser.add_argument(
        "--amc-commit-url",
        default="http://127.0.0.1:8000/api/v1/amc/commit",
        help="AMC commit endpoint used after merged trajectory is collected",
    )
    parser.add_argument(
        "--amc-commit-timeout",
        type=float,
        default=300.0,
        help="Timeout (seconds) for AMC commit request",
    )
    parser.add_argument(
        "--default-account-id",
        default="openclaw",
        help="Fallback account_id when ingest header X-Account-Id is missing",
    )
    parser.add_argument(
        "--default-agent-id",
        default="openclaw-sidecar",
        help="Fallback agent_id when ingest header X-Agent-Id is missing",
    )
    parser.add_argument(
        "--amc-retrieve-url",
        default="http://127.0.0.1:8000/api/v1/amc/retrieve",
        help="AMC retrieve endpoint used by assemble",
    )
    parser.add_argument(
        "--amc-retrieve-timeout",
        type=float,
        default=20.0,
        help="Timeout (seconds) for AMC retrieve request in assemble",
    )
    args = parser.parse_args(argv)

    global _output_dir
    global _amc_commit_url, _amc_commit_timeout_s
    global _amc_retrieve_url, _amc_retrieve_timeout_s
    global _default_account_id, _default_agent_id
    _output_dir = Path(args.output_dir).resolve()
    _output_dir.mkdir(parents=True, exist_ok=True)
    _amc_commit_url = str(args.amc_commit_url).strip()
    _amc_commit_timeout_s = float(args.amc_commit_timeout)
    _amc_retrieve_url = str(args.amc_retrieve_url).strip()
    _amc_retrieve_timeout_s = float(args.amc_retrieve_timeout)
    _default_account_id = str(args.default_account_id).strip() or "openclaw"
    _default_agent_id = str(args.default_agent_id).strip() or "openclaw-sidecar"

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()

