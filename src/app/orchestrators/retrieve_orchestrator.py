"""Retrieve orchestrator: API boundary for retrieve pipeline."""

from __future__ import annotations

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

    def retrieve(self, command: RetrieveCommand) -> RetrieveResult:
        result = self.retrieve_service.run(command)
        # Enrich with local FS summaries; clean graph is loaded from graph backend only.
        for item in result.items:
            tid = str(item.get("trajectory_id") or "")
            bundle = self.repo.load_trajectory(tid) if tid else None
            if bundle:
                item["abstract"] = str(bundle.get("abstract") or "")
                item["overview"] = str(bundle.get("overview") or "")
            item["clean_graph"] = self.clean_graph_loader(tid) if (tid and self.clean_graph_loader) else None
        self.audit.write(
            action="retrieve",
            result="accepted",
            details={
                "tenant_id": command.tenant_id,
                "agent_id": command.agent_id,
                "top_k": command.top_k,
                "hit_count": len(result.items),
            },
        )
        return result
