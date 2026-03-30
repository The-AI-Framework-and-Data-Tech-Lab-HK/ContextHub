"""Retrieve orchestrator: API boundary for retrieve pipeline."""

from __future__ import annotations

import json
from typing import Any, Callable

from core.retrieve.service import RetrieveCommand, RetrieveResult, RetrieveService
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository


class RetrieveOrchestrator:
    def __init__(
        self,
        *,
        retrieve_service: RetrieveService,
        repo: LocalFSTrajectoryRepository,
        audit: JsonlAuditLogger,
        clean_graph_loader: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.retrieve_service = retrieve_service
        self.repo = repo
        self.audit = audit
        self.clean_graph_loader = clean_graph_loader

    @staticmethod
    def _truncate_field(value: Any, max_chars: int = 200) -> Any:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
        if len(text) <= max_chars:
            return value
        return text[:max_chars] + f"...(truncated,{len(text)-max_chars} chars)"

    @classmethod
    def _compact_clean_graph(cls, clean_graph: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(clean_graph, dict):
            return None
        nodes_raw = clean_graph.get("nodes")
        edges_raw = clean_graph.get("edges")
        nodes_out: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []

        for n in nodes_raw if isinstance(nodes_raw, list) else []:
            if not isinstance(n, dict):
                continue
            tool_args = n.get("tool_args")
            compact_args: Any = tool_args
            if isinstance(tool_args, dict):
                compact_args = {
                    str(k): cls._truncate_field(v, max_chars=200) for k, v in tool_args.items()
                }
            tool_output = n.get("tool_output")
            compact_output: Any = tool_output
            if isinstance(tool_output, dict):
                compact_output = {
                    str(k): cls._truncate_field(v, max_chars=200) for k, v in tool_output.items()
                }
            nodes_out.append(
                {
                    "node_id": n.get("node_id"),
                    "thinking": str(n.get("thinking") or ""),
                    "tool_name": n.get("tool_name"),
                    "tool_args": compact_args,
                    "tool_output": compact_output,
                }
            )

        for e in edges_raw if isinstance(edges_raw, list) else []:
            if not isinstance(e, dict):
                continue
            edges_out.append(
                {
                    "src": e.get("src"),
                    "dst": e.get("dst"),
                    "dep_type": e.get("dep_type"),
                }
            )
        return {"nodes": nodes_out, "edges": edges_out}

    def retrieve(self, command: RetrieveCommand) -> RetrieveResult:
        result = self.retrieve_service.run(command)
        # Enrich with local FS summaries; clean graph is loaded from graph backend only.
        for item in result.items:
            tid = str(item.get("trajectory_id") or "")
            bundle = self.repo.load_trajectory(tid) if tid else None
            if bundle:
                item["abstract"] = str(bundle.get("abstract") or "")
                item["overview"] = str(bundle.get("overview") or "")
            graph = self.clean_graph_loader(tid) if (tid and self.clean_graph_loader) else None
            if command.include_full_clean_graph:
                item["clean_graph"] = graph
            else:
                item["clean_graph"] = self._compact_clean_graph(graph)
        self.audit.write(
            action="retrieve",
            result="accepted",
            details={
                "account_id": command.account_id,
                "agent_id": command.agent_id,
                "scope_filter": list(command.scope_filter or []),
                "owner_space_filter": list(command.owner_space_filter or []),
                "top_k": command.top_k,
                "hit_count": len(result.items),
            },
        )
        return result
