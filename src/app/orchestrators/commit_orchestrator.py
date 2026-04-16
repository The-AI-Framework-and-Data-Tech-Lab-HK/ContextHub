"""Commit orchestrator: ACL/audit-friendly boundary for API layer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from core.commit.service import CommitCommand, CommitResult, CommitService
from core.indexing.base import TrajectoryIndexer
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.base import GraphStoreWriter


@dataclass
class PreparedCommitOutcome:
    command: CommitCommand
    result: CommitResult | None
    error: Exception | None = None


class CommitOrchestrator:
    def __init__(
        self,
        *,
        commit_service: CommitService,
        repo: LocalFSTrajectoryRepository,
        audit: JsonlAuditLogger,
        graph_store: GraphStoreWriter | None = None,
        vector_indexer: TrajectoryIndexer | None = None,
        idempotency_enabled: bool = False,
    ) -> None:
        self.commit_service = commit_service
        self.repo = repo
        self.audit = audit
        self.graph_store = graph_store
        self.vector_indexer = vector_indexer
        self.idempotency_enabled = idempotency_enabled

    def _persist_prepared_commit(self, command: CommitCommand, result: CommitResult) -> CommitResult:
        # Persist prepared result into graph/fs/vector backends.
        account_id = command.resolved_account_id()
        scope = command.resolved_scope()
        owner_space = command.resolved_owner_space()
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
            vector_summary: dict[str, Any] = {"enabled": False}
            if self.vector_indexer is not None and bundle and bundle.get("base_path"):
                try:
                    vector_summary = self.vector_indexer.index_trajectory(
                        agent_id=command.agent_id,
                        account_id=account_id,
                        scope=scope,
                        owner_space=owner_space,
                        trajectory_id=existing_id,
                        base_path=str(bundle["base_path"]),
                        lifecycle_status="active",
                        stale_flag=False,
                    )
                except Exception as exc:
                    vector_summary = {
                        "enabled": True,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
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
            out.payload["vector_index_summary"] = vector_summary
            self.audit.write(
                action="commit",
                result="idempotent",
                details={
                    "agent_id": command.agent_id,
                    "account_id": account_id,
                    "scope": scope,
                    "owner_space": owner_space,
                    "task_id": command.task_id,
                    "trajectory_id": existing_id,
                },
            )
            return out

        # First-seen commit: persist trajectory + raw/clean graph artifacts.
        neo4j_summary: dict[str, Any] | None = None
        if self.graph_store is not None:
            neo4j_summary = self.graph_store.upsert_trajectory_graphs(
                agent_id=command.agent_id,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=result.trajectory_id,
                raw_graph=result.payload["raw_graph"],
                clean_graph=result.payload["clean_graph"],
            )
        result.payload["neo4j_summary"] = neo4j_summary or {"enabled": False}
        base_path = self.repo.save_bundle(
            agent_id=command.agent_id,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=result.trajectory_id,
            idempotency_key=result.idempotency_key,
            payload=result.payload,
            visualize_graph_png=command.visualize_graph_png,
        )
        vector_summary: dict[str, Any] = {"enabled": False}
        if self.vector_indexer is not None:
            try:
                vector_summary = self.vector_indexer.index_trajectory(
                    agent_id=command.agent_id,
                    account_id=account_id,
                    scope=scope,
                    owner_space=owner_space,
                    trajectory_id=result.trajectory_id,
                    base_path=base_path,
                    lifecycle_status="active",
                    stale_flag=False,
                )
            except Exception as exc:
                result.warnings.append(f"vector indexing skipped: {type(exc).__name__}")
                vector_summary = {"enabled": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result.payload["vector_index_summary"] = vector_summary
        self.audit.write(
            action="commit",
            result="accepted" if self.idempotency_enabled else "accepted_idempotency_disabled",
            details={
                "agent_id": command.agent_id,
                "account_id": account_id,
                "scope": scope,
                "owner_space": owner_space,
                "task_id": command.task_id,
                "trajectory_id": result.trajectory_id,
                "nodes": result.nodes,
                "edges": result.edges,
                "idempotency_enabled": self.idempotency_enabled,
            },
        )
        return result

    def prepare_commit(self, command: CommitCommand) -> CommitResult:
        # Service computes graph + summaries and returns deterministic idempotency key.
        return self.commit_service.run(command)

    def prepare_commits(
        self, commands: list[CommitCommand], *, max_workers: int = 1
    ) -> list[PreparedCommitOutcome]:
        if not commands:
            return []
        workers = max(1, int(max_workers))
        if workers == 1:
            outcomes: list[PreparedCommitOutcome] = []
            for command in commands:
                try:
                    outcomes.append(
                        PreparedCommitOutcome(
                            command=command,
                            result=self.prepare_commit(command),
                            error=None,
                        )
                    )
                except Exception as exc:
                    outcomes.append(
                        PreparedCommitOutcome(command=command, result=None, error=exc)
                    )
            return outcomes

        outcomes_by_idx: dict[int, PreparedCommitOutcome] = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(commands))) as pool:
            future_to_idx = {
                pool.submit(self.prepare_commit, command): idx for idx, command in enumerate(commands)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                command = commands[idx]
                try:
                    outcomes_by_idx[idx] = PreparedCommitOutcome(
                        command=command,
                        result=fut.result(),
                        error=None,
                    )
                except Exception as exc:
                    outcomes_by_idx[idx] = PreparedCommitOutcome(
                        command=command, result=None, error=exc
                    )
        return [outcomes_by_idx[i] for i in range(len(commands))]

    def commit(self, command: CommitCommand) -> CommitResult:
        prepared = self.prepare_commit(command)
        return self._persist_prepared_commit(command, prepared)

    def commit_prepared(self, command: CommitCommand, result: CommitResult) -> CommitResult:
        return self._persist_prepared_commit(command, result)

    def replay(self, trajectory_id: str) -> dict[str, Any] | None:
        # Thin pass-through for API replay endpoint.
        return self.repo.load_trajectory(trajectory_id)
