"""Normalize tool action strings and truncate large tool outputs."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


def _sanitize_tool_name(name: str) -> str:
    lowered = (name or "").strip().lower().replace("/", " ")
    # Remove ALFWorld placeholder markers like "_" from "put _ in/on".
    lowered = re.sub(r"\b_\b", " ", lowered)
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    return lowered.strip("_")


def _parse_placeholder_action(action: str) -> dict[str, Any] | None:
    """
    Parse placeholder-style actions used by ALFWorld-like traces, e.g.:
    - ``go to _(recep="toiletpaperhanger 1")``
    - ``take _ from _(obj="apple 1", recep="table 1")``
    - ``put _ in/on _(obj="x", recep="y")``
    """
    text = (action or "").strip()
    if not text or not text.endswith(")"):
        return None

    marker = "_("
    idx = text.rfind(marker)
    if idx <= 0:
        return None

    prefix = text[:idx].strip()
    args_src = text[idx + len(marker) : -1].strip()
    tool_name = _sanitize_tool_name(prefix)
    if not tool_name:
        return None

    parsed: dict[str, Any] = {"tool_name": tool_name}
    if not args_src:
        return parsed

    try:
        fake_call = ast.parse(f"f({args_src})", mode="eval").body
    except SyntaxError:
        return parsed

    if not isinstance(fake_call, ast.Call):
        return parsed

    def _safe_eval(expr: ast.expr) -> Any:
        try:
            return ast.literal_eval(expr)
        except Exception:
            return ast.unparse(expr)

    for i, arg in enumerate(fake_call.args):
        parsed[f"arg{i}"] = _safe_eval(arg)
    for kw in fake_call.keywords:
        if kw.arg is None:
            parsed["kwargs"] = _safe_eval(kw.value)
        else:
            parsed[kw.arg] = _safe_eval(kw.value)
    return parsed


def parse_local_db_sql_action(action: str) -> dict[str, Any] | None:
    """
    Parse generic function-call action strings, e.g.:
    - ``local_db_sql(file_path="...", command="...")``
    - ``run_query(sql="SELECT 1", timeout=5)``
    - ``pkg.tool(arg1, mode="fast")``

    Returns None when input is not a valid function call expression.
    """
    if not action or not action.strip():
        return None

    text = action.strip()
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        # Compatibility: ALFWorld-like actions use placeholder syntax such as
        # "go to _(recep='...')" which is not valid Python call syntax.
        return _parse_placeholder_action(text)

    if not isinstance(node, ast.Call):
        return None

    def _func_name(func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            # Keep the right-most callable name for readability in graph labels.
            return func.attr
        return None

    def _safe_eval(expr: ast.expr) -> Any:
        try:
            return ast.literal_eval(expr)
        except Exception:
            # Keep non-literal expressions as source text.
            return ast.unparse(expr)

    tool_name = _func_name(node.func)
    if not tool_name:
        return None

    parsed: dict[str, Any] = {"tool_name": tool_name}

    # Positional arguments are preserved as arg0/arg1/... for generic tools.
    for i, arg in enumerate(node.args):
        parsed[f"arg{i}"] = _safe_eval(arg)

    # Keyword arguments keep original names.
    for kw in node.keywords:
        if kw.arg is None:  # **kwargs
            parsed["kwargs"] = _safe_eval(kw.value)
        else:
            parsed[kw.arg] = _safe_eval(kw.value)

    return parsed


def truncate_tool_output(text: str, max_chars: int) -> tuple[str, bool]:
    """
    If *text* exceeds *max_chars*, keep a head/tail summary. Returns (text, was_truncated).
    """
    if len(text) <= max_chars:
        return text, False
    head = max_chars // 2
    tail = max_chars - head - 30
    snippet = f"{text[:head]}\n... [truncated {len(text) - max_chars} chars] ...\n{text[-tail:]}"
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 3] + "..."
    return snippet, True


def parse_tool_output_to_dict(action_result: Any) -> dict[str, Any] | None:
    """
    Normalize ToolMessage output into dict for downstream edge building.

    Priority:
    1) dict -> return as-is
    2) JSON string -> parsed dict
    3) Python-literal string -> parsed dict (supports sample_traj single-quote payloads)
    4) fallback -> {"raw_text": "..."}
    """
    if action_result is None:
        return None
    if isinstance(action_result, dict):
        return action_result

    text = str(action_result).strip()
    if not text:
        return None

    try:
        parsed_json = json.loads(text)
        if isinstance(parsed_json, dict):
            return parsed_json
        return {"value": parsed_json}
    except Exception:
        pass

    try:
        parsed_py = ast.literal_eval(text)
        if isinstance(parsed_py, dict):
            return parsed_py
        return {"value": parsed_py}
    except Exception:
        return {"raw_text": text}
