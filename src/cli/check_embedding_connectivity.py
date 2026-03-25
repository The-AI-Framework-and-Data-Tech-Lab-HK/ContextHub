"""Check AMC embedding connectivity with current config/env."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.config import load_settings
import httpx
from openai import OpenAI


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _embedding_probe(api_key: str, base_url: str | None, model: str) -> dict[str, Any]:
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    resp = client.embeddings.create(
        model=model,
        input="amc embedding probe",
    )
    emb = resp.data[0].embedding if resp.data else []
    dim = len(emb) if isinstance(emb, list) else 0
    return {
        "ok": True,
        "response_id": getattr(resp, "id", ""),
        "embedding_dim": dim,
    }


def _extract_embedding_dim(payload: dict[str, Any]) -> int:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        row = data[0] if isinstance(data[0], dict) else {}
        emb = row.get("embedding")
        if isinstance(emb, list):
            return len(emb)
    if isinstance(data, dict):
        emb = data.get("embedding")
        if isinstance(emb, list):
            return len(emb)
    return 0


def _embedding_probe_multimodal(api_key: str, base_url: str | None, model: str) -> dict[str, Any]:
    if not base_url:
        raise ValueError("AMC_EMBEDDING_BASE_URL is required for multimodal probe mode")
    endpoint = f"{base_url.rstrip('/')}/embeddings/multimodal"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Different gateways can require slightly different payload schemas.
    # Try a few common text-only multimodal payload forms for diagnostics.
    candidate_payloads: list[dict[str, Any]] = [
        {
            "model": model,
            "input": [
                {
                    "type": "text",
                    "text": "amc embedding probe",
                }
            ],
            "encoding_format": "float",
        },
        {
            "model": model,
            "input": [
                {
                    "type": "text",
                    "text": "amc embedding probe",
                }
            ],
        },
        {
            "model": model,
            "input": "amc embedding probe",
            "encoding_format": "float",
        },
    ]
    last_error: str = "unknown multimodal probe failure"
    with httpx.Client(timeout=30.0) as client:
        for payload in candidate_payloads:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text}"
                continue
            data = resp.json()
            dim = _extract_embedding_dim(data)
            return {
                "ok": True,
                "response_id": str(data.get("id", "")),
                "created": data.get("created"),
                "embedding_dim": dim,
                "probe_endpoint": endpoint,
                "probe_payload_shape": payload,
            }
    raise RuntimeError(last_error)


def run_check(config_path: str | None = None, mode: str = "text") -> dict[str, Any]:
    settings = load_settings(config_path=config_path)
    api_key = settings.openai_api_key
    base_url = settings.model_endpoints.embedder_base_url or None
    model = settings.embedding_model
    provider = settings.embedding_provider

    result: dict[str, Any] = {
        "effective_config": {
            "embedding_provider": provider,
            "embedding_model": model,
            "embedding_base_url": base_url or "",
            "embedding_probe_mode": mode,
            "openai_api_key_masked": _mask_key(api_key),
            "vector_store_backend": settings.vector_store_backend,
            "vector_collection_name": settings.vector_collection_name,
        },
        "embedding_probe": {"ok": False},
    }

    if provider.lower() != "openai":
        result["error"] = f"unsupported embedding provider: {provider}"
        return result

    if not api_key:
        result["error"] = "AMC_OPENAI_API_KEY is empty"
        return result

    try:
        if mode == "multimodal":
            result["embedding_probe"] = _embedding_probe_multimodal(api_key, base_url, model)
        else:
            result["embedding_probe"] = _embedding_probe(api_key, base_url, model)
    except Exception as exc:  # pragma: no cover - runtime connectivity diagnostics
        result["embedding_probe"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-check-embedding",
        description="Check whether AMC can call configured embedding endpoint/model.",
    )
    parser.add_argument("--config-path", default=None, help="Optional config YAML path")
    parser.add_argument(
        "--mode",
        choices=["text", "multimodal"],
        default="text",
        help="Probe mode. text uses OpenAI /embeddings; multimodal uses /embeddings/multimodal.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = run_check(config_path=args.config_path, mode=args.mode)
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
