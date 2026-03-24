"""U-03 / U-04: action parsing and truncation (AMC_plan/13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.commit.normalizer import (
    parse_local_db_sql_action,
    parse_tool_output_to_dict,
    truncate_tool_output,
)

pytestmark = pytest.mark.unit


def test_u03_parse_traj1_local_db_sql(sample_traj_dir: Path) -> None:
    path = sample_traj_dir / "traj1.json"
    if not path.exists():
        pytest.skip("traj1 missing")
    steps = json.loads(path.read_text(encoding="utf-8"))
    ai_with_action = next(s for s in steps if s.get("meta", {}).get("role") == "AIMessage" and s.get("Action"))
    parsed = parse_local_db_sql_action(str(ai_with_action["Action"]))
    assert parsed is not None
    assert parsed["tool_name"] == "local_db_sql"
    assert "file_path" in parsed
    assert "command" in parsed
    assert parsed["file_path"].endswith(".sqlite")


def test_u04_truncate_long_action_result(sample_traj_dir: Path) -> None:
    path = sample_traj_dir / "traj2.json"
    if not path.exists():
        pytest.skip("traj2 missing")
    steps = json.loads(path.read_text(encoding="utf-8"))
    long_text = ""
    for s in steps:
        if s.get("meta", {}).get("role") == "ToolMessage":
            ar = str(s.get("Action_result") or "")
            if len(ar) > len(long_text):
                long_text = ar
    assert len(long_text) > 200
    max_chars = 400
    out, truncated = truncate_tool_output(long_text, max_chars=max_chars)
    assert truncated is True
    assert len(out) <= max_chars
    assert "truncated" in out


def test_parse_generic_function_name_and_kwargs() -> None:
    parsed = parse_local_db_sql_action("execute_sql(query='select 1', timeout=10)")
    assert parsed is not None
    assert parsed["tool_name"] == "execute_sql"
    assert parsed["query"] == "select 1"
    assert parsed["timeout"] == 10


def test_parse_tool_output_to_dict_from_python_literal_string() -> None:
    parsed = parse_tool_output_to_dict("{'status': 'success', 'rows': 3}")
    assert parsed is not None
    assert isinstance(parsed, dict)
    assert parsed["status"] == "success"
    assert parsed["rows"] == 3
