"""Commit orchestrator: ACL/audit-friendly boundary for API layer."""

from __future__ import annotations

from typing import Any

from core.commit.service import CommitCommand, CommitResult, CommitService
from core.indexing.base import TrajectoryIndexer
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.base import GraphStoreWriter


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

    def commit(self, command: CommitCommand) -> CommitResult:
        # Service computes graph + summaries and returns deterministic idempotency key.
        result = self.commit_service.run(command)
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
                        tenant_id=command.tenant_id,
                        agent_id=command.agent_id,
                        account_id=account_id,
                        scope=scope,
                        owner_space=owner_space,
                        trajectory_id=existing_id,
                        task_type=str(command.labels.get("task_type", "") or ""),
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
                    "tenant_id": command.tenant_id,
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
                tenant_id=command.tenant_id,
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
            tenant_id=command.tenant_id,
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
                    tenant_id=command.tenant_id,
                    agent_id=command.agent_id,
                    account_id=account_id,
                    scope=scope,
                    owner_space=owner_space,
                    trajectory_id=result.trajectory_id,
                    task_type=str(command.labels.get("task_type", "") or ""),
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
                "tenant_id": command.tenant_id,
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

    def replay(self, trajectory_id: str) -> dict[str, Any] | None:
        # Thin pass-through for API replay endpoint.
        return self.repo.load_trajectory(trajectory_id)
