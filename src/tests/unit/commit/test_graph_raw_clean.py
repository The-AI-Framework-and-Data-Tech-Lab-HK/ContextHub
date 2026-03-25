"""U-05–U-07, U-08: raw/clean graph, edge types, deterministic IDs (AMC_plan/13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.commit.clean_deriver import derive_clean_graph
from core.commit.graph_builder import build_raw_graph, strip_echoed_output_payload
from core.commit.pairing import pair_ai_tool_steps

pytestmark = pytest.mark.unit


def _load(name: str, sample_traj_dir: Path) -> list:
    p = sample_traj_dir / name
    if not p.exists():
        pytest.skip(f"missing {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def test_u05_traj1_raw_contains_failed_and_retry(sample_traj_dir: Path) -> None:
    steps = _load("traj1.json", sample_traj_dir)
    tid = "test-tenant-traj1"
    pairs = pair_ai_tool_steps(steps)
    nodes, edges = build_raw_graph(tid, pairs)

    statuses = {n.output_status for n in nodes}
    assert "failed" in statuses
    assert "success" in statuses

    retry_edges = [e for e in edges if e.dep_type == "retry" and e.signal == "retry"]
    assert retry_edges, "expected retry edge on failed SQL then fix (traj1)"

    # Tool output from ToolMessage should be carried into node payload.
    assert any(n.tool_output for n in nodes), "expected at least one node with tool_output"
    assert all((n.tool_output is None or isinstance(n.tool_output, dict)) for n in nodes)
    # Thinking content from AIMessage should be preserved in graph nodes.
    assert all(isinstance(n.thinking, str) for n in nodes)


def test_u06_clean_no_more_nodes_than_raw(sample_traj_dir: Path) -> None:
    steps = _load("traj1.json", sample_traj_dir)
    tid = "test-traj1-clean"
    pairs = pair_ai_tool_steps(steps)
    raw_nodes, raw_edges = build_raw_graph(tid, pairs)
    clean_nodes, _clean_edges = derive_clean_graph(raw_nodes, raw_edges)
    assert len(clean_nodes) <= len(raw_nodes)


def test_u06_failed_superseded_removed_from_clean(sample_traj_dir: Path) -> None:
    steps = _load("traj1.json", sample_traj_dir)
    tid = "test-traj1-superseded"
    pairs = pair_ai_tool_steps(steps)
    raw_nodes, raw_edges = build_raw_graph(tid, pairs)
    clean_nodes, _ = derive_clean_graph(raw_nodes, raw_edges)
    failed_kept = [n for n in clean_nodes if n.output_status == "failed"]
    assert not failed_kept, "failed nodes with later success should be dropped from clean"


def test_u07_temporal_or_dataflow_present(sample_traj_dir: Path) -> None:
    for fname in ("traj1.json", "traj2.json"):
        steps = _load(fname, sample_traj_dir)
        tid = f"test-{fname}"
        pairs = pair_ai_tool_steps(steps)
        _nodes, edges = build_raw_graph(tid, pairs)
        types = {e.dep_type for e in edges}
        assert "temporal" in types
        assert "dataflow" in types or "temporal" in types
        for e in edges:
            if e.dep_type == "dataflow":
                assert e.confidence >= 0.45
                assert e.signal_detail is not None
                assert "matched_tokens" in e.signal_detail
            if e.dep_type == "temporal":
                assert e.confidence <= 0.35


def test_u07_traj1_has_retry_edge(sample_traj_dir: Path) -> None:
    steps = _load("traj1.json", sample_traj_dir)
    _nodes, edges = build_raw_graph("tid", pair_ai_tool_steps(steps))
    assert any(e.dep_type == "retry" for e in edges)


def test_u08_node_ids_deterministic(sample_traj_dir: Path) -> None:
    steps = _load("traj5.json", sample_traj_dir)
    tid = "stable-trajectory-id"
    pairs = pair_ai_tool_steps(steps)
    n1, _ = build_raw_graph(tid, pairs)
    n2, _ = build_raw_graph(tid, pairs)
    assert [x.node_id for x in n1] == [x.node_id for x in n2]


def test_temporal_fallback_can_be_disabled() -> None:
    steps = [
        {
            "Step": 1,
            "Thinking": "",
            "Action": "tool_a(x='foo')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 2,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status': 'success', 'data': {'value': 1}}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
        {
            "Step": 3,
            "Thinking": "",
            "Action": "tool_b(y='bar')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 4,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status': 'success'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
    ]
    pairs = pair_ai_tool_steps(steps)
    _nodes, edges = build_raw_graph("tid-no-temporal", pairs, temporal_fallback_edge=False)
    assert not any(e.dep_type == "temporal" for e in edges)


def test_echoed_input_in_tool_output_not_counted_as_dataflow() -> None:
    """
    If output only echoes input (e.g. file_path -> db_path same value),
    it should not be treated as a new dataflow signal.
    """
    shared_path = "/tmp/demo.sqlite"
    steps = [
        {
            "Step": 1,
            "Thinking": "",
            "Action": f"local_db_sql(file_path='{shared_path}', command='SELECT 1')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 2,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status': 'success', 'db_path': '/tmp/demo.sqlite'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
        {
            "Step": 3,
            "Thinking": "",
            "Action": f"local_db_sql(file_path='{shared_path}', command='PRAGMA table_info(t)')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 4,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status': 'success'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
    ]
    pairs = pair_ai_tool_steps(steps)
    _nodes, edges = build_raw_graph("tid-echo-filter", pairs, temporal_fallback_edge=True)
    dataflow_edges = [e for e in edges if e.dep_type == "dataflow"]
    assert not dataflow_edges, "echoed input should not create dataflow edge"
    assert any(e.dep_type == "temporal" for e in edges), "temporal fallback should still apply"


def test_strip_echoed_output_payload_removes_overlapping_scalars() -> None:
    args = {"file_path": "/tmp/a.sqlite", "command": "SELECT 1"}
    out = {"status": "success", "db_path": "/tmp/a.sqlite", "rows": 1}
    effective = strip_echoed_output_payload(out, args)
    assert effective is not None
    assert "db_path" not in effective
    assert effective.get("status") == "success"


def test_llm_dataflow_extractor_path_allows_partial_match_dependency() -> None:
    steps = [
        {
            "Step": 1,
            "Thinking": "",
            "Action": "tool_a()",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 2,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success','data':['ch___company_info']}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
        {
            "Step": 3,
            "Thinking": "",
            "Action": "tool_b(command='SELECT * FROM ch___company_info LIMIT 5')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 4,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
    ]
    pairs = pair_ai_tool_steps(steps)

    def _fake_extractor(*, nodes: list[dict], threshold: float, top_k_per_dst: int) -> list[dict]:
        assert threshold == 0.45
        assert top_k_per_dst == 2
        assert nodes[0]["effective_tool_output"] is not None
        return [
            {
                "src_node_id": nodes[0]["node_id"],
                "dst_node_id": nodes[1]["node_id"],
                "confidence": 0.88,
                "evidence_type": "enum_to_command",
                "matched_tokens": ["ch___company_info"],
                "reason": "output token appears inside later SQL command string",
            }
        ]

    _nodes, edges = build_raw_graph(
        "tid-llm-dataflow",
        pairs,
        temporal_fallback_edge=True,
        dataflow_extractor=_fake_extractor,
    )
    df = [e for e in edges if e.dep_type == "dataflow"]
    assert df, "fake llm extractor should create dataflow edge"
    assert any("ch___company_info" in (e.signal_detail or {}).get("matched_tokens", []) for e in df)


def test_llm_extractor_keyword_only_signature_is_supported() -> None:
    steps = [
        {
            "Step": 1,
            "Thinking": "",
            "Action": "tool_a()",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 2,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success','data':['token_x']}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
        {
            "Step": 3,
            "Thinking": "",
            "Action": "tool_b(command='use token_x')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 4,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
    ]
    pairs = pair_ai_tool_steps(steps)

    def _kw_only_extractor(*, nodes: list[dict], threshold: float, top_k_per_dst: int) -> list[dict]:
        assert nodes
        assert threshold == 0.45
        assert top_k_per_dst == 2
        return [
            {
                "src_node_id": nodes[0]["node_id"],
                "dst_node_id": nodes[1]["node_id"],
                "confidence": 0.9,
                "evidence_type": "kw_only",
                "matched_tokens": ["token_x"],
                "reason": "keyword-only extractor works",
            }
        ]

    _nodes, edges = build_raw_graph(
        "tid-kw-only-extractor",
        pairs,
        temporal_fallback_edge=True,
        dataflow_extractor=_kw_only_extractor,
    )
    assert any(e.dep_type == "dataflow" for e in edges)


def test_llm_dataflow_requires_token_in_source_effective_output() -> None:
    steps = [
        {
            "Step": 1,
            "Thinking": "",
            "Action": "tool_a(x='foo')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 2,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success','data':['only_output_token']}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
        {
            "Step": 3,
            "Thinking": "",
            "Action": "tool_b(command='consume arg_only_token')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 4,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
    ]
    pairs = pair_ai_tool_steps(steps)

    def _fake_extractor(*, nodes: list[dict], threshold: float, top_k_per_dst: int) -> list[dict]:
        _ = threshold, top_k_per_dst
        return [
            {
                "src_node_id": nodes[0]["node_id"],
                "dst_node_id": nodes[1]["node_id"],
                "confidence": 0.9,
                "evidence_type": "bad_provenance",
                "matched_tokens": ["arg_only_token"],
                "reason": "incorrectly inferred from src tool args",
            }
        ]

    _nodes, edges = build_raw_graph(
        "tid-llm-guard",
        pairs,
        temporal_fallback_edge=True,
        dataflow_extractor=_fake_extractor,
    )
    assert not any(e.dep_type == "dataflow" for e in edges)
    assert any(e.dep_type == "temporal" for e in edges)


def test_llm_reasoning_edge_is_added_when_evidence_in_output_and_thinking() -> None:
    steps = [
        {
            "Step": 1,
            "Thinking": "",
            "Action": "tool_a()",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 2,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success','data':['token_y']}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
        {
            "Step": 3,
            "Thinking": "I should use token_y from previous results.",
            "Action": "tool_b(command='consume token_y')",
            "Action_result": "",
            "Response": "",
            "meta": {"role": "AIMessage"},
        },
        {
            "Step": 4,
            "Thinking": "",
            "Action": "",
            "Action_result": "{'status':'success'}",
            "Response": "",
            "meta": {"role": "ToolMessage"},
        },
    ]
    pairs = pair_ai_tool_steps(steps)

    def _fake_joint_extractor(
        *,
        nodes: list[dict],
        threshold: float,
        top_k_per_dst: int,
        reasoning_threshold: float,
    ) -> dict[str, list[dict]]:
        _ = threshold, top_k_per_dst, reasoning_threshold
        return {
            "dataflow_edges": [],
            "reasoning_edges": [
                {
                    "src_node_id": nodes[0]["node_id"],
                    "dst_node_id": nodes[1]["node_id"],
                    "confidence": 0.86,
                    "reason_summary": "thinking mentions token from prior output",
                    "matched_evidence": ["token_y"],
                }
            ],
        }

    _nodes, edges = build_raw_graph(
        "tid-llm-reasoning",
        pairs,
        temporal_fallback_edge=True,
        dataflow_extractor=_fake_joint_extractor,
    )
    reasoning_edges = [e for e in edges if e.dep_type == "reasoning"]
    assert reasoning_edges
    detail = reasoning_edges[0].signal_detail or {}
    assert detail.get("reason_summary")
    assert "token_y" in (detail.get("matched_evidence") or [])
