"""Commit orchestrator: ACL/audit-friendly boundary for API layer."""

from __future__ import annotations

from typing import Any

from core.commit.service import CommitCommand, CommitResult, CommitService
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository


class CommitOrchestrator:
    def __init__(
        self,
        *,
        commit_service: CommitService,
        repo: LocalFSTrajectoryRepository,
        audit: JsonlAuditLogger,
        idempotency_enabled: bool = False,
    ) -> None:
        self.commit_service = commit_service
        self.repo = repo
        self.audit = audit
        self.idempotency_enabled = idempotency_enabled

    def commit(self, command: CommitCommand) -> CommitResult:
        # Service computes graph + summaries and returns deterministic idempotency key.
        result = self.commit_service.run(command)
        existing_id = (
            self.repo.find_trajectory_id_by_idempotency_key(result.idempotency_key)
            if self.idempotency_enabled
            else None
        )
        if existing_id:
            # Duplicate commit: return stable result without rewriting storage.
            bundle = self.repo.load_trajectory(existing_id)
            nodes = int((bundle or {}).get("meta", {}).get("nodes", 0))
            edges = int((bundle or {}).get("meta", {}).get("edges", 0))
            out = CommitResult(
                trajectory_id=existing_id,
                idempotency_key=result.idempotency_key,
                status="idempotent",
                nodes=nodes,
                edges=edges,
                warnings=["duplicate commit skipped via idempotency key"],
                summary_l0=(bundle or {}).get("abstract", result.summary_l0),
                summary_l1=(bundle or {}).get("overview", result.summary_l1),
                payload=result.payload,
            )
            self.audit.write(
                action="commit",
                result="idempotent",
                details={
                    "tenant_id": command.tenant_id,
                    "agent_id": command.agent_id,
                    "task_id": command.task_id,
                    "trajectory_id": existing_id,
                },
            )
            return out

        # First-seen commit: persist trajectory + raw/clean graph artifacts.
        self.repo.save_bundle(
            tenant_id=command.tenant_id,
            agent_id=command.agent_id,
            trajectory_id=result.trajectory_id,
            idempotency_key=result.idempotency_key,
            payload=result.payload,
            visualize_graph_png=command.visualize_graph_png,
        )
        self.audit.write(
            action="commit",
            result="accepted" if self.idempotency_enabled else "accepted_idempotency_disabled",
            details={
                "tenant_id": command.tenant_id,
                "agent_id": command.agent_id,
                "task_id": command.task_id,
                "trajectory_id": result.trajectory_id,
                "nodes": result.nodes,
                "edges": result.edges,
                "idempotency_enabled": self.idempotency_enabled,
            },
        )
        return result

    def replay(self, trajectory_id: str) -> dict[str, Any] | None:
        # Thin pass-through for API replay endpoint.
        return self.repo.load_trajectory(trajectory_id)
