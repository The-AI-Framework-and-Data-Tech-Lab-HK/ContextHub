"""Check AMC LLM connectivity with current config/env."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.config import load_settings
from core.commit.dataflow_llm import LLMDataflowExtractor
from openai import OpenAI


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _base_chat_probe(api_key: str, base_url: str | None, model: str) -> dict[str, Any]:
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": "Reply with one word: pong"},
            {"role": "user", "content": "ping"},
        ],
    )
    content = resp.choices[0].message.content or ""
    return {
        "ok": True,
        "response_text": content.strip(),
        "response_id": getattr(resp, "id", ""),
    }


def _dataflow_probe(api_key: str, base_url: str | None, model: str) -> dict[str, Any]:
    extractor = LLMDataflowExtractor(api_key=api_key, base_url=base_url, model=model, temperature=0.0)
    nodes = [
        {
            "node_id": "n0",
            "ai_step": 1,
            "tool_step": 2,
            "tool_name": "discover_tables",
            "tool_args": {"query": "list tables"},
            "tool_output": {"status": "success", "data": ["ch___company_info"]},
            "effective_tool_output": {"status": "success", "data": ["ch___company_info"]},
            "output_status": "success",
        },
        {
            "node_id": "n1",
            "ai_step": 3,
            "tool_step": 4,
            "tool_name": "local_db_sql",
            "tool_args": {"command": "SELECT * FROM ch___company_info LIMIT 5"},
            "tool_output": {"status": "success"},
            "effective_tool_output": {"status": "success"},
            "output_status": "success",
        },
    ]
    result = extractor.extract(nodes=nodes, threshold=0.45, top_k_per_dst=2, reasoning_threshold=0.55)
    dataflow_edges = result.get("dataflow_edges", [])
    reasoning_edges = result.get("reasoning_edges", [])
    return {
        "ok": True,
        "dataflow_edge_count": len(dataflow_edges),
        "reasoning_edge_count": len(reasoning_edges),
        "dataflow_edges_preview": dataflow_edges[:2],
        "reasoning_edges_preview": reasoning_edges[:2],
    }


def run_check(config_path: str | None = None) -> dict[str, Any]:
    settings = load_settings(config_path=config_path)
    api_key = settings.openai_api_key
    base_url = settings.model_endpoints.llm_base_url or None
    model = settings.llm_model

    result: dict[str, Any] = {
        "effective_config": {
            "dataflow_extractor": settings.commit.dataflow_extractor,
            "llm_model": model,
            "llm_base_url": base_url or "",
            "openai_api_key_masked": _mask_key(api_key),
        },
        "base_chat_probe": {"ok": False},
        "dataflow_extractor_probe": {"ok": False},
    }

    if not api_key:
        result["error"] = "AMC_OPENAI_API_KEY is empty"
        return result

    try:
        result["base_chat_probe"] = _base_chat_probe(api_key, base_url, model)
    except Exception as exc:  # pragma: no cover - runtime connectivity diagnostics
        result["base_chat_probe"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    try:
        result["dataflow_extractor_probe"] = _dataflow_probe(api_key, base_url, model)
    except Exception as exc:  # pragma: no cover - runtime connectivity diagnostics
        result["dataflow_extractor_probe"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-check-llm",
        description="Check whether AMC can call configured LLM endpoint/model.",
    )
    parser.add_argument("--config-path", default=None, help="Optional config YAML path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = run_check(config_path=args.config_path)
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
