"""Promote orchestrator for agent->team trajectory sharing."""

from __future__ import annotations

from core.promote.service import PromoteCommand, PromoteResult
from core.indexing.base import TrajectoryIndexer
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository


class PromoteOrchestrator:
    def __init__(
        self,
        *,
        repo: LocalFSTrajectoryRepository,
        audit: JsonlAuditLogger,
        vector_indexer: TrajectoryIndexer | None = None,
    ) -> None:
        self.repo = repo
        self.audit = audit
        self.vector_indexer = vector_indexer

    def promote(self, command: PromoteCommand) -> PromoteResult:
        promoted = self.repo.promote_bundle_to_team(
            account_id=command.account_id,
            source_agent_id=command.agent_id,
            promoted_by_agent_id=command.agent_id,
            source_trajectory_id=command.trajectory_id,
            target_team=command.target_team,
        )

        vector_summary = {"enabled": False}
        if self.vector_indexer is not None:
            # Ensure promoted team URI is searchable in vector recall.
            vector_summary = self.vector_indexer.index_trajectory(
                agent_id=command.agent_id,
                account_id=command.account_id,
                scope="team",
                owner_space=command.target_team,
                trajectory_id=command.trajectory_id,
                base_path=str(promoted["base_path"]),
                lifecycle_status="active",
                stale_flag=False,
                force_reindex=True,
            )

        self.audit.write(
            action="promote_trajectory",
            result="accepted",
            details={
                "account_id": command.account_id,
                "agent_id": command.agent_id,
                "trajectory_id": command.trajectory_id,
                "target_team": command.target_team,
                "source_uri": promoted["source_uri"],
                "target_uri": promoted["target_uri"],
                "reason": command.reason or "",
                "vector_index_summary": vector_summary,
            },
        )
        return PromoteResult(
            source_uri=str(promoted["source_uri"]),
            target_uri=str(promoted["target_uri"]),
            trajectory_id=str(promoted["trajectory_id"]),
            scope=str(promoted["scope"]),
            owner_space=str(promoted["owner_space"]),
            vector_index_summary=vector_summary,
        )
