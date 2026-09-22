#!/usr/bin/env python3
"""Probe OpenAI-compatible model latency without printing secrets.

Examples:
    OPENAI_BASE_URL=https://example.com/v1 OPENAI_API_KEY=... \
      python scripts/probe_model_latency.py --models gpt-5-mini deepseek-v4-flash --runs 3

    python scripts/probe_model_latency.py --config model_providers.local.json --runs 3
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_PROMPT = (
    "Reply with exactly one short sentence explaining why latency probes should use the same prompt."
)


@dataclass(frozen=True)
class ProbeTarget:
    model: str
    base_url: str
    api_key: str
    label: str


@dataclass(frozen=True)
class ProbeRun:
    ok: bool
    total_s: float
    first_token_s: float | None = None
    output_chars: int = 0
    error: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", help="Model names for OPENAI_BASE_URL/OPENAI_API_KEY.")
    parser.add_argument("--config", type=Path, help="JSON file with per-provider model configs.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per model.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Request timeout in seconds.")
    parser.add_argument("--max-tokens", type=int, default=64, help="Max output tokens.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt used for every probe.")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming/first-token timing.")
    args = parser.parse_args()

    targets = _load_targets(args)
    if not targets:
        print(
            "No targets found. Provide --models with OPENAI_BASE_URL/OPENAI_API_KEY, "
            "or pass --config model_providers.local.json.",
            file=sys.stderr,
        )
        return 2

    rows: list[dict[str, Any]] = []
    for target in targets:
        runs = [
            probe_once(
                target,
                prompt=args.prompt,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                stream=not args.no_stream,
            )
            for _ in range(max(1, args.runs))
        ]
        rows.append(_summarize(target, runs))

    _print_table(rows)
    return 0


def _load_targets(args: argparse.Namespace) -> list[ProbeTarget]:
    targets: list[ProbeTarget] = []
    if args.config is not None:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        for item in data.get("targets", data if isinstance(data, list) else []):
            base_url = str(item.get("base_url") or "").strip()
            api_key = str(item.get("api_key") or "").strip()
            for model in item.get("models") or [item.get("model")]:
                if model and base_url and api_key:
                    label = str(item.get("label") or _host_label(base_url))
                    targets.append(ProbeTarget(str(model), base_url, api_key, label))

    if args.models:
        base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        for model in args.models:
            if base_url and api_key:
                targets.append(ProbeTarget(model, base_url, api_key, _host_label(base_url)))
    return targets


def probe_once(
    target: ProbeTarget,
    *,
    prompt: str,
    timeout: float,
    max_tokens: int,
    stream: bool,
) -> ProbeRun:
    started = time.perf_counter()
    try:
        if stream:
            return _probe_streaming(target, prompt=prompt, timeout=timeout, max_tokens=max_tokens, started=started)
        return _probe_non_streaming(target, prompt=prompt, timeout=timeout, max_tokens=max_tokens, started=started)
    except Exception as exc:
        return ProbeRun(
            ok=False,
            total_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _probe_streaming(
    target: ProbeTarget,
    *,
    prompt: str,
    timeout: float,
    max_tokens: int,
    started: float,
) -> ProbeRun:
    first_token_s: float | None = None
    output: list[str] = []
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST",
            _chat_url(target.base_url),
            headers=_headers(target.api_key),
            json=_payload(target.model, prompt, max_tokens=max_tokens, stream=True),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue
                chunk = json.loads(data)
                content = (
                    chunk.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if content:
                    if first_token_s is None:
                        first_token_s = time.perf_counter() - started
                    output.append(str(content))
    return ProbeRun(
        ok=True,
        total_s=time.perf_counter() - started,
        first_token_s=first_token_s,
        output_chars=len("".join(output)),
    )


def _probe_non_streaming(
    target: ProbeTarget,
    *,
    prompt: str,
    timeout: float,
    max_tokens: int,
    started: float,
) -> ProbeRun:
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            _chat_url(target.base_url),
            headers=_headers(target.api_key),
            json=_payload(target.model, prompt, max_tokens=max_tokens, stream=False),
        )
        response.raise_for_status()
        data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content") or ""
    return ProbeRun(ok=True, total_s=time.perf_counter() - started, output_chars=len(str(content)))


def _payload(model: str, prompt: str, *, max_tokens: int, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": stream,
    }


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _host_label(base_url: str) -> str:
    without_scheme = base_url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def _summarize(target: ProbeTarget, runs: list[ProbeRun]) -> dict[str, Any]:
    ok_runs = [run for run in runs if run.ok]
    totals = [run.total_s for run in ok_runs]
    first_tokens = [run.first_token_s for run in ok_runs if run.first_token_s is not None]
    errors = [run.error for run in runs if run.error]
    return {
        "provider": target.label,
        "model": target.model,
        "ok": len(ok_runs),
        "total": len(runs),
        "median_total_s": statistics.median(totals) if totals else None,
        "min_total_s": min(totals) if totals else None,
        "max_total_s": max(totals) if totals else None,
        "median_first_token_s": statistics.median(first_tokens) if first_tokens else None,
        "median_output_chars": statistics.median([run.output_chars for run in ok_runs]) if ok_runs else None,
        "error": errors[0] if errors else "",
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        "provider",
        "model",
        "ok",
        "median_total_s",
        "median_first_token_s",
        "min_total_s",
        "max_total_s",
        "median_output_chars",
        "error",
    ]
    widths = {header: len(header) for header in headers}
    rendered: list[dict[str, str]] = []
    for row in rows:
        rendered_row = {
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "ok": f"{row['ok']}/{row['total']}",
            "median_total_s": _fmt(row["median_total_s"]),
            "median_first_token_s": _fmt(row["median_first_token_s"]),
            "min_total_s": _fmt(row["min_total_s"]),
            "max_total_s": _fmt(row["max_total_s"]),
            "median_output_chars": _fmt(row["median_output_chars"], digits=0),
            "error": str(row["error"])[:80],
        }
        rendered.append(rendered_row)
        for header, value in rendered_row.items():
            widths[header] = max(widths[header], len(value))

    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rendered:
        print("  ".join(row[header].ljust(widths[header]) for header in headers))


def _fmt(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
