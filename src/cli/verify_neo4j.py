"""Commit trajectory and verify Neo4j graph artifacts."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import load_settings
from cli.commit_trajectory import run_commit
from neo4j import GraphDatabase


@contextmanager
def _temporary_env(overrides: dict[str, str | None]) -> Iterator[None]:
    old: dict[str, str | None] = {}
    for key, value in overrides.items():
        old[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def verify(
    *,
    trajectory_file: Path,
    account_id: str,
    agent_id: str,
    session_id: str,
    task_id: str | None,
    config_path: str | None,
    disable_idempotency: bool,
    force_rule_based: bool,
) -> dict[str, Any]:
    env_overrides = {"AMC_COMMIT_DATAFLOW_EXTRACTOR": "rule_based"} if force_rule_based else {}
    with _temporary_env(env_overrides):
        commit_out = run_commit(
            trajectory_file=trajectory_file,
            account_id=account_id,
            agent_id=agent_id,
            session_id=session_id,
            task_id=task_id,
            trajectory_id=None,
            visualize_graph_png=False,
            disable_idempotency=disable_idempotency,
            config_path=config_path,
        )

    settings = load_settings(config_path=config_path)
    if not (settings.neo4j_uri and settings.neo4j_user and settings.neo4j_password):
        raise RuntimeError("Neo4j config missing: AMC_NEO4J_URI/USER/PASSWORD are required")

    tid = str(commit_out["trajectory_id"])
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        with driver.session(database=settings.neo4j_database) as session:
            traj_row = session.run(
                "MATCH (t:AMCTrajectory {trajectory_id:$tid}) RETURN t.account_id AS account_id, t.agent_id AS agent",
                tid=tid,
            ).single()
            node_counts = session.run(
                """
                MATCH (n:AMCNode {trajectory_id:$tid})
                RETURN count(n) AS total,
                       count(CASE WHEN n:RawNode THEN 1 END) AS raw_nodes,
                       count(CASE WHEN n:CleanNode THEN 1 END) AS clean_nodes,
                       min(n.ai_step) AS min_ai_step,
                       max(n.ai_step) AS max_ai_step
                """,
                tid=tid,
            ).single()
            edge_counts = list(
                session.run(
                    """
                    MATCH (:AMCNode {trajectory_id:$tid})-[r]->(:AMCNode {trajectory_id:$tid})
                    RETURN type(r) AS rel_type, count(*) AS count
                    ORDER BY rel_type
                    """,
                    tid=tid,
                )
            )
            labels_by_kind = list(
                session.run(
                    """
                    MATCH (n:AMCNode {trajectory_id:$tid})
                    RETURN n.graph_kind AS graph_kind, collect(DISTINCT labels(n)) AS label_sets, count(*) AS count
                    ORDER BY graph_kind
                    """,
                    tid=tid,
                )
            )
            rel_dep_mapping = list(
                session.run(
                    """
                    MATCH (:AMCNode {trajectory_id:$tid})-[r]->(:AMCNode {trajectory_id:$tid})
                    RETURN DISTINCT type(r) AS rel_type, collect(DISTINCT r.dep_type) AS dep_types
                    ORDER BY rel_type
                    """,
                    tid=tid,
                )
            )
            sample_edges = list(
                session.run(
                    """
                    MATCH (a:AMCNode {trajectory_id:$tid})-[r]->(b:AMCNode {trajectory_id:$tid})
                    RETURN r.graph_kind AS graph_kind,
                           type(r) AS rel_type,
                           r.dep_type AS dep_type,
                           a.node_id AS src,
                           b.node_id AS dst,
                           r.confidence AS confidence,
                           r.signal AS signal
                    ORDER BY rel_type, confidence DESC
                    LIMIT 10
                    """,
                    tid=tid,
                )
            )
    finally:
        driver.close()

    return {
        "commit": {
            "status": commit_out["status"],
            "trajectory_id": tid,
            "nodes": commit_out["nodes"],
            "edges": commit_out["edges"],
            "raw_graph_path": commit_out["storage"]["raw_graph_path"],
            "clean_graph_path": commit_out["storage"]["clean_graph_path"],
        },
        "neo4j": {
            "trajectory_node": dict(traj_row) if traj_row else {},
            "node_counts": dict(node_counts) if node_counts else {},
            "edge_type_counts": [dict(r) for r in edge_counts],
            "labels_by_kind": [dict(r) for r in labels_by_kind],
            "relation_dep_mapping": [dict(r) for r in rel_dep_mapping],
            "sample_edges": [dict(r) for r in sample_edges],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-verify-neo4j",
        description="Commit a trajectory then verify Neo4j node/edge labels and relation types.",
    )
    parser.add_argument("trajectory_file", help="Path to trajectory JSON (e.g. sample_traj/traj1.json)")
    parser.add_argument("--account-id", default="account-local", help="Account identifier")
    parser.add_argument("--agent-id", default="agent-local", help="Agent identifier")
    parser.add_argument("--session-id", default="session-local", help="Session identifier")
    parser.add_argument("--task-id", default=None, help="Task id (default: task-<filename>)")
    parser.add_argument("--disable-idempotency", action="store_true", help="Force overwrite behavior")
    parser.add_argument(
        "--force-rule-based",
        action="store_true",
        help="Temporarily force AMC_COMMIT_DATAFLOW_EXTRACTOR=rule_based for faster verification",
    )
    parser.add_argument("--config-path", default=None, help="Optional config YAML path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = verify(
        trajectory_file=Path(args.trajectory_file),
        account_id=args.account_id,
        agent_id=args.agent_id,
        session_id=args.session_id,
        task_id=args.task_id,
        config_path=args.config_path,
        disable_idempotency=args.disable_idempotency,
        force_rule_based=args.force_rule_based,
    )
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
