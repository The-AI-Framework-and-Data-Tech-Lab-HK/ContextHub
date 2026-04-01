"""AMC v0 sidecar for OpenClaw context-engine bridge.

v0 behavior:
- ingest: write full incoming message payload to local files under openclaw_message/
- assemble: return empty context
- compact: delegated by TS bridge to OpenClaw runtime (not handled here)
"""

from __future__ import annotations

import argparse
import json
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
        "session_id": session_id,
        "account_id": account_id,
        "agent_id": agent_id,
        "source": "trajectory_merged",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "trajectory_started_at": records[0].get("received_at"),
        "trajectory_ended_at": records[-1].get("received_at"),
        "finished_reason": finished_reason,
        "message_count": len(records),
        # Keep merged trajectory compact with single-level content blocks.
        "messages": flattened_contents,
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

