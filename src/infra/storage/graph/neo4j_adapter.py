"""Neo4j graph writer for AMC trajectory raw/clean graphs."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from neo4j import GraphDatabase


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def _json_load(text: Any) -> Any:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


class Neo4jGraphWriter:
    """
    Persist trajectory graphs in Neo4j with explicit labels and relation types.

    Labels:
    - :AMCTrajectory
    - :AMCNode + :RawNode / :CleanNode

    Relationship types:
    - :DATAFLOW | :REASONING | :TEMPORAL | :RETRY
    """

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
    ) -> None:
        self.database = database
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def upsert_trajectory_graphs(
        self,
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        raw_graph: dict[str, Any],
        clean_graph: dict[str, Any],
    ) -> dict[str, Any]:
        raw = _to_jsonable(raw_graph)
        clean = _to_jsonable(clean_graph)
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._clear_trajectory_graph,
                trajectory_id=trajectory_id,
            )
            session.execute_write(
                self._merge_trajectory_node,
                agent_id=agent_id,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=trajectory_id,
            )
            self._write_graph_kind(session, trajectory_id=trajectory_id, graph_kind="raw", graph=raw)
            self._write_graph_kind(session, trajectory_id=trajectory_id, graph_kind="clean", graph=clean)
        return self._build_summary(
            agent_id=agent_id,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
            database=self.database,
            raw_graph=raw,
            clean_graph=clean,
        )

    def load_clean_graph(self, *, trajectory_id: str) -> dict[str, Any] | None:
        with self.driver.session(database=self.database) as session:
            nodes_result = session.run(
                """
                MATCH (n:AMCNode:CleanNode {trajectory_id:$trajectory_id, graph_kind:'clean'})
                RETURN n
                ORDER BY n.ai_step, n.tool_step, n.node_id
                """,
                trajectory_id=trajectory_id,
            )
            nodes: list[dict[str, Any]] = []
            for rec in nodes_result:
                n = rec.get("n")
                if n is None:
                    continue
                item = dict(n)
                nodes.append(
                    {
                        "node_id": item.get("node_id"),
                        "trajectory_id": item.get("trajectory_id"),
                        "ai_step": item.get("ai_step"),
                        "tool_step": item.get("tool_step"),
                        "thinking": item.get("thinking") or "",
                        "tool_name": item.get("tool_name"),
                        "tool_args": _json_load(item.get("tool_args_json")),
                        "tool_output": _json_load(item.get("tool_output_json")),
                        "output_status": item.get("output_status"),
                        "pending_output": bool(item.get("pending_output")),
                        "quality_flags": item.get("quality_flags") or [],
                    }
                )

            edges_result = session.run(
                """
                MATCH (src:AMCNode:CleanNode {trajectory_id:$trajectory_id, graph_kind:'clean'})
                      -[r]->
                      (dst:AMCNode:CleanNode {trajectory_id:$trajectory_id, graph_kind:'clean'})
                RETURN src.node_id AS src_node_id, dst.node_id AS dst_node_id, r
                ORDER BY r.edge_id
                """,
                trajectory_id=trajectory_id,
            )
            edges: list[dict[str, Any]] = []
            for rec in edges_result:
                r = rec.get("r")
                if r is None:
                    continue
                rel = dict(r)
                edges.append(
                    {
                        "edge_id": rel.get("edge_id"),
                        "src": rec.get("src_node_id"),
                        "dst": rec.get("dst_node_id"),
                        "dep_type": rel.get("dep_type"),
                        "signal": rel.get("signal"),
                        "confidence": rel.get("confidence"),
                        "signal_detail": _json_load(rel.get("signal_detail_json")),
                    }
                )

        if not nodes and not edges:
            return None
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _build_summary(
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        database: str,
        raw_graph: dict[str, Any],
        clean_graph: dict[str, Any],
    ) -> dict[str, Any]:
        raw_edges = [e for e in (raw_graph.get("edges") or []) if isinstance(e, dict)]
        clean_edges = [e for e in (clean_graph.get("edges") or []) if isinstance(e, dict)]
        raw_rel_counts: dict[str, int] = {}
        clean_rel_counts: dict[str, int] = {}
        for edge in raw_edges:
            rel_type = Neo4jGraphWriter._relationship_type(str(edge.get("dep_type") or "TEMPORAL"))
            raw_rel_counts[rel_type] = raw_rel_counts.get(rel_type, 0) + 1
        for edge in clean_edges:
            rel_type = Neo4jGraphWriter._relationship_type(str(edge.get("dep_type") or "TEMPORAL"))
            clean_rel_counts[rel_type] = clean_rel_counts.get(rel_type, 0) + 1
        return {
            "enabled": True,
            "agent_id": agent_id,
            "account_id": account_id,
            "scope": scope,
            "owner_space": owner_space,
            "trajectory_id": trajectory_id,
            "database": database,
            "raw_nodes": len(raw_graph.get("nodes") or []),
            "raw_edges": len(raw_edges),
            "raw_edge_type_counts": raw_rel_counts,
            "clean_nodes": len(clean_graph.get("nodes") or []),
            "clean_edges": len(clean_edges),
            "clean_edge_type_counts": clean_rel_counts,
        }

    @staticmethod
    def _clear_trajectory_graph(tx: Any, *, trajectory_id: str) -> None:
        tx.run(
            """
            MATCH (n:AMCNode {trajectory_id: $trajectory_id})
            DETACH DELETE n
            """,
            trajectory_id=trajectory_id,
        )

    @staticmethod
    def _merge_trajectory_node(
        tx: Any,
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> None:
        tx.run(
            """
            MERGE (t:AMCTrajectory {trajectory_id: $trajectory_id})
            SET t.agent_id = $agent_id,
                t.account_id = $account_id,
                t.scope = $scope,
                t.owner_space = $owner_space
            """,
            trajectory_id=trajectory_id,
            agent_id=agent_id,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
        )

    def _write_graph_kind(
        self,
        session: Any,
        *,
        trajectory_id: str,
        graph_kind: str,
        graph: dict[str, Any],
    ) -> None:
        node_label = "RawNode" if graph_kind == "raw" else "CleanNode"
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            session.execute_write(
                self._merge_node,
                trajectory_id=trajectory_id,
                graph_kind=graph_kind,
                node_label=node_label,
                node=node,
            )
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            rel_type = self._relationship_type(str(edge.get("dep_type") or "TEMPORAL"))
            session.execute_write(
                self._merge_edge,
                trajectory_id=trajectory_id,
                graph_kind=graph_kind,
                rel_type=rel_type,
                edge=edge,
            )

    @staticmethod
    def _relationship_type(dep_type: str) -> str:
        key = dep_type.strip().lower()
        if key == "dataflow":
            return "DATAFLOW"
        if key == "reasoning":
            return "REASONING"
        if key == "retry":
            return "RETRY"
        if key == "controlflow":
            # Backward compatibility for older graphs.
            return "RETRY"
        return "TEMPORAL"

    @staticmethod
    def _merge_node(
        tx: Any,
        *,
        trajectory_id: str,
        graph_kind: str,
        node_label: str,
        node: dict[str, Any],
    ) -> None:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            return
        graph_node_id = f"{trajectory_id}:{graph_kind}:{node_id}"
        query = f"""
        MATCH (t:AMCTrajectory {{trajectory_id: $trajectory_id}})
        MERGE (n:AMCNode:{node_label} {{graph_node_id: $graph_node_id}})
        SET n.trajectory_id = $trajectory_id,
            n.graph_kind = $graph_kind,
            n.node_id = $node_id,
            n.ai_step = $ai_step,
            n.tool_step = $tool_step,
            n.tool_name = $tool_name,
            n.output_status = $output_status,
            n.pending_output = $pending_output,
            n.thinking = $thinking,
            n.tool_args_json = $tool_args_json,
            n.tool_output_json = $tool_output_json,
            n.quality_flags = $quality_flags
        MERGE (t)-[:HAS_NODE {{graph_kind: $graph_kind}}]->(n)
        """
        tx.run(
            query,
            trajectory_id=trajectory_id,
            graph_node_id=graph_node_id,
            graph_kind=graph_kind,
            node_id=node_id,
            ai_step=int(node.get("ai_step") or 0),
            tool_step=node.get("tool_step"),
            tool_name=node.get("tool_name"),
            output_status=node.get("output_status"),
            pending_output=bool(node.get("pending_output")),
            thinking=str(node.get("thinking") or ""),
            tool_args_json=_json_text(node.get("tool_args")),
            tool_output_json=_json_text(node.get("tool_output")),
            quality_flags=[str(x) for x in (node.get("quality_flags") or [])],
        )

    @staticmethod
    def _merge_edge(
        tx: Any,
        *,
        trajectory_id: str,
        graph_kind: str,
        rel_type: str,
        edge: dict[str, Any],
    ) -> None:
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        edge_id = str(edge.get("edge_id") or "")
        if not src or not dst or not edge_id:
            return
        src_graph_node_id = f"{trajectory_id}:{graph_kind}:{src}"
        dst_graph_node_id = f"{trajectory_id}:{graph_kind}:{dst}"
        graph_edge_id = f"{trajectory_id}:{graph_kind}:{edge_id}"
        query = f"""
        MATCH (src:AMCNode {{graph_node_id: $src_graph_node_id}})
        MATCH (dst:AMCNode {{graph_node_id: $dst_graph_node_id}})
        MERGE (src)-[r:{rel_type} {{graph_edge_id: $graph_edge_id}}]->(dst)
        SET r.trajectory_id = $trajectory_id,
            r.graph_kind = $graph_kind,
            r.edge_id = $edge_id,
            r.dep_type = $dep_type,
            r.signal = $signal,
            r.confidence = $confidence,
            r.signal_detail_json = $signal_detail_json
        """
        tx.run(
            query,
            src_graph_node_id=src_graph_node_id,
            dst_graph_node_id=dst_graph_node_id,
            graph_edge_id=graph_edge_id,
            trajectory_id=trajectory_id,
            graph_kind=graph_kind,
            edge_id=edge_id,
            dep_type=str(edge.get("dep_type") or ""),
            signal=edge.get("signal"),
            confidence=float(edge.get("confidence") or 0.0),
            signal_detail_json=_json_text(edge.get("signal_detail")),
        )
