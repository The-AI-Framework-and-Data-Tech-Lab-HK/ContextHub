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
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="AMC v0 Sidecar")

_output_dir = Path("/home/qchenax/ContextHub/openclaw_message")
_seq = count(1)
_traj_seq = count(1)
_session_buffers: dict[str, list[dict[str, Any]]] = {}
_trajectory_subdir = "trajectories"


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


def _strip_query_timestamp_prefix(query: str) -> str:
    # Example: "[Wed 2026-04-01 17:40 GMT+8] Count the number ..."
    text = (query or "").strip()
    return re.sub(r"^\[[^\]]+\]\s*", "", text).strip()


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


def _write_trajectory_snapshot(
    *,
    session_id: str,
    account_id: str,
    agent_id: str,
    records: list[dict[str, Any]],
    finished_reason: str,
) -> None:
    if not records:
        return
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

    # v0 rule: only persist merged trajectories that contain at least one toolCall.
    has_tool_call = any(
        isinstance(block, dict) and str(block.get("type") or "") == "toolCall"
        for block in flattened_contents
    )
    if not has_tool_call:
        return

    payload: dict[str, Any] = {
        "query": _extract_query(records),
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
    (out_dir / f"{ts}_{seq:06d}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
async def ingest(request: Request) -> dict[str, bool]:
    body = await request.json()
    session_id = str(body.get("sessionId") or "unknown_session")
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S.%fZ")
    seq = next(_seq)
    agent_id = request.headers.get("x-agent-id", "")
    account_id = request.headers.get("x-account-id", "")

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

    # If a new user message arrives before previous trajectory closed, flush stale buffer.
    if role == "user" and buffer:
        _write_trajectory_snapshot(
            session_id=session_id,
            account_id=account_id,
            agent_id=agent_id,
            records=buffer,
            finished_reason="new_user_start_without_stop",
        )
        _cleanup_single_message_files(buffer)
        buffer.clear()

    buffer.append(payload)
    if stop_reason == "stop":
        _write_trajectory_snapshot(
            session_id=session_id,
            account_id=account_id,
            agent_id=agent_id,
            records=buffer,
            finished_reason="stop",
        )
        _cleanup_single_message_files(buffer)
        buffer.clear()

    return {"ingested": True}


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
    args = parser.parse_args(argv)

    global _output_dir
    _output_dir = Path(args.output_dir).resolve()
    _output_dir.mkdir(parents=True, exist_ok=True)

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()

