#!/usr/bin/env python3
"""Minimal provider probe for AgentLeak Phase 5 (ContextHub side).

Makes a SINGLE cheap chat completion against the OpenAI-compatible provider
described by ``AGENTLEAK_PROVIDER_CONFIG`` (e.g. ContextHub's
``model_providers.local.json``) to confirm a model slug can be called before any
benchmark run.

Safety:
- Never prints or persists the API key value (only ``api_key_present`` boolean).
- Never starts the AgentLeak benchmark.
- Prints only: model, base_url domain, ok/error class, latency, token counts,
  and the response length (not the response text).

Usage:
    AGENTLEAK_PROVIDER_CONFIG=/path/to/model_providers.local.json \
        python integrations/agentleak/probe_provider.py --model deepseek-v4-flash
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse


def _load_target() -> dict:
    cfg = os.getenv("AGENTLEAK_PROVIDER_CONFIG")
    if not cfg:
        raise SystemExit("AGENTLEAK_PROVIDER_CONFIG is not set")
    path = Path(cfg).expanduser()
    if not path.exists():
        raise SystemExit(f"config not found: {cfg}")
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = data.get("targets") or []
    label = os.getenv("AGENTLEAK_PROVIDER_LABEL")
    if label:
        target = next((t for t in targets if t.get("label") == label), None)
        if target is None:
            raise SystemExit(f"no target labeled {label!r}")
    else:
        target = targets[0]
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=16)
    args = parser.parse_args(argv)

    target = _load_target()
    base_url = str(target.get("base_url") or "")
    api_key = target.get("api_key")
    models = [str(m) for m in (target.get("models") or [])]

    report = {
        "model": args.model,
        "provider_label": target.get("label"),
        "base_url_domain": urlparse(base_url).netloc or base_url,
        "api_key_present": bool(api_key),
        "model_in_config": args.model in models,
        "probe_status": "not_run",
    }

    if not api_key:
        report["probe_status"] = "failed"
        report["error_class"] = "no_api_key"
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    try:
        from openai import OpenAI

        client = OpenAI(api_key=str(api_key), base_url=base_url)
        start = time.time()
        resp = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0.0,
            max_tokens=args.max_tokens,
        )
        latency_ms = round((time.time() - start) * 1000, 1)
        text = resp.choices[0].message.content or ""
        report.update(
            {
                "probe_status": "passed",
                "latency_ms": latency_ms,
                "response_len": len(text),
                "tokens_prompt": getattr(getattr(resp, "usage", None), "prompt_tokens", None),
                "tokens_completion": getattr(getattr(resp, "usage", None), "completion_tokens", None),
                "finish_reason": getattr(resp.choices[0], "finish_reason", None),
            }
        )
        # Diagnostics when standard content is empty: locate where output went,
        # by structure only (field names + lengths), never the text itself.
        if not text:
            msg = resp.choices[0].message
            extra: dict = {}
            for attr in ("reasoning", "reasoning_content"):
                val = getattr(msg, attr, None)
                if val:
                    extra[attr + "_len"] = len(str(val))
            model_extra = getattr(msg, "model_extra", None)
            if isinstance(model_extra, dict):
                extra["message_extra_keys"] = sorted(model_extra.keys())
                for k, v in model_extra.items():
                    if isinstance(v, str) and v:
                        extra[f"extra.{k}_len"] = len(v)
            report["empty_content_diagnostics"] = extra
    except Exception as exc:  # noqa: BLE001 - report class only, not key/value
        report["probe_status"] = "failed"
        report["error_class"] = type(exc).__name__
        # Keep a short, non-sensitive message; truncate to avoid echoing payloads.
        report["error_brief"] = str(exc)[:200]

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["probe_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
