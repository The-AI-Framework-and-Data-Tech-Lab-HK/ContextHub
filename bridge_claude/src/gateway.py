from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI


def _read_jiuwen_env_var(name: str) -> str:
    env_path = Path(os.getenv("JIUWEN_ENV_FILE", str(Path.home() / ".jiuwenclaw" / "config" / ".env")))
    if not env_path.exists():
        return ""
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        return value.strip().strip('"').strip("'")
    return ""


def _cfg(name: str, fallback: str = "") -> str:
    return os.getenv(name) or _read_jiuwen_env_var(name) or fallback


DEEPSEEK_API_BASE = _cfg("DEEPSEEK_API_BASE", _cfg("API_BASE", "https://api.deepseek.com")).rstrip("/")
DEEPSEEK_API_KEY = _cfg("DEEPSEEK_API_KEY", _cfg("API_KEY"))
DEEPSEEK_MODEL_NAME = _cfg("DEEPSEEK_MODEL_NAME", _cfg("MODEL_NAME", "deepseek-chat"))
GATEWAY_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN", "local-deepseek-gateway")
PORT = int(os.getenv("BRIDGE_CLAUDE_GATEWAY_PORT", "8787"))

app = FastAPI(title="ContextHub Claude DeepSeek Gateway")


def _require_auth(authorization: str | None) -> None:
    if not GATEWAY_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != GATEWAY_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "tool_result":
                    content = item.get("content", "")
                    parts.append(_flatten_text(content))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _flatten_text(value["content"])
    return str(value)


def anthropic_to_openai_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    system = body.get("system")
    system_text = _flatten_text(system).strip()
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            messages.append({"role": role, "content": _flatten_text(content)})
            continue

        text_parts: list[str] = []
        assistant_tool_calls: list[dict[str, Any]] = []

        for block in content:
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue

            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif role == "user" and block_type == "tool_result":
                tool_result_content = _flatten_text(block.get("content", ""))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": tool_result_content,
                    }
                )
            elif role == "assistant" and block_type == "tool_use":
                assistant_tool_calls.append(
                    {
                        "id": block.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    }
                )

        if role == "assistant" and assistant_tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": "\n".join(text_parts).strip() or None,
                    "tool_calls": assistant_tool_calls,
                }
            )
        elif text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts).strip()})

    return messages


def anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


def build_openai_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": body.get("model") or DEEPSEEK_MODEL_NAME,
        "messages": anthropic_to_openai_messages(body),
        "stream": False,
    }
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    tools = anthropic_tools_to_openai(body.get("tools"))
    if tools:
        payload["tools"] = tools
    return payload


def openai_message_to_anthropic_content(message: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    content: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})

    stop_reason = "end_turn"
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        args_text = function.get("arguments") or "{}"
        try:
            args_obj = json.loads(args_text)
        except Exception:
            args_obj = {"raw": args_text}
        content.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                "name": function.get("name", ""),
                "input": args_obj,
            }
        )
        stop_reason = "tool_use"

    if not content:
        content.append({"type": "text", "text": ""})
    return content, stop_reason


def openai_response_to_anthropic(data: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content, stop_reason = openai_message_to_anthropic_content(message)
    usage = data.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def anthropic_sse_from_message(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    chunks.append(_sse("message_start", {"type": "message_start", "message": message}))

    for index, block in enumerate(message.get("content", [])):
        chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": block}))
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                chunks.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )
        elif block.get("type") == "tool_use":
            input_json = json.dumps(block.get("input", {}), ensure_ascii=False)
            chunks.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": input_json},
                    },
                )
            )
        chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))

    chunks.append(
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": message.get("stop_reason"),
                    "stop_sequence": None,
                },
                "usage": message.get("usage", {}),
            },
        )
    )
    chunks.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(chunks)


def client() -> AsyncOpenAI:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek API key is missing.")
    return AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_API_BASE,
        max_retries=1,
        timeout=60,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "deepseek_base": DEEPSEEK_API_BASE, "model": DEEPSEEK_MODEL_NAME}


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(authorization)
    return {
        "data": [
            {
                "id": DEEPSEEK_MODEL_NAME,
                "type": "model",
                "display_name": DEEPSEEK_MODEL_NAME,
                "created_at": int(time.time()),
            }
        ]
    }


@app.post("/v1/messages")
async def messages(request: Request, authorization: str | None = Header(default=None)) -> Any:
    _require_auth(authorization)
    body = await request.json()
    requested_model = body.get("model") or DEEPSEEK_MODEL_NAME
    stream = bool(body.get("stream"))
    payload = build_openai_payload(body)
    payload["model"] = requested_model

    async with client() as c:
        response = await c.chat.completions.create(**payload)
        data = response.model_dump(mode="json")

    message = openai_response_to_anthropic(data, requested_model)
    if not stream:
        return JSONResponse(content=message)
    return StreamingResponse(iter([anthropic_sse_from_message(message)]), media_type="text/event-stream")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()

