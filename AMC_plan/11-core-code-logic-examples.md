# 11 — AMC 核心代码逻辑示意（commit / retrieve / feedback）

本文件承接 `10-main-code-structure.md`，提供“可落地的示意实现”。
目标是明确主流程和模块交互，不追求可直接运行。

---

## 11.1 CommitOrchestrator（主流程）

```python
from dataclasses import dataclass
from typing import Any


@dataclass
class CommitCommand:
    tenant_id: str
    agent_id: str
    session_id: str
    task_id: str
    trajectory: list[dict[str, Any]]
    labels: dict[str, Any]
    is_incremental: bool = False
    trajectory_id: str | None = None


@dataclass
class CommitResult:
    trajectory_id: str
    nodes: int
    edges: int
    status: str
    warnings: list[str]


class CommitOrchestrator:
    def __init__(
        self,
        acl_engine,
        validator,
        normalizer,
        pairing,
        graph_builder,
        clean_deriver,
        summarizer,
        deps_extractor,
        fs_repo,
        graph_store,
        event_log,
        audit_logger,
        embedding_queue,
        idempotency_repo,
    ):
        self.acl = acl_engine
        self.validator = validator
        self.normalizer = normalizer
        self.pairing = pairing
        self.graph_builder = graph_builder
        self.clean_deriver = clean_deriver
        self.summarizer = summarizer
        self.deps_extractor = deps_extractor
        self.fs_repo = fs_repo
        self.graph_store = graph_store
        self.event_log = event_log
        self.audit_logger = audit_logger
        self.embedding_queue = embedding_queue
        self.idempotency_repo = idempotency_repo

    async def handle(self, cmd: CommitCommand, actor: str) -> CommitResult:
        await self.acl.assert_allowed(
            principal=actor,
            action="commit",
            resource=f"ctx://agent/{cmd.agent_id}/memories/trajectories/*",
        )

        self.validator.validate_request(cmd)
        idem_key = self.validator.build_idempotency_key(cmd)
        old = await self.idempotency_repo.get(idem_key)
        if old:
            return CommitResult(
                trajectory_id=old["trajectory_id"],
                nodes=old["nodes"],
                edges=old["edges"],
                status="dedup",
                warnings=["duplicate_commit"],
            )

        warnings: list[str] = []
        trajectory_id = cmd.trajectory_id or self.validator.new_trajectory_id(cmd)
        norm_steps, parse_warnings = self.normalizer.normalize_steps(cmd.trajectory)
        warnings.extend(parse_warnings)

        nodes = self.pairing.pair_ai_tool_to_nodes(trajectory_id=trajectory_id, normalized_steps=norm_steps)
        raw_graph = self.graph_builder.build_raw(nodes)
        clean_graph = self.clean_deriver.derive_from_raw(raw_graph)
        l0, l1, failure_signature = self.summarizer.build_trajectory_l0_l1(nodes, raw_graph, clean_graph)
        deps = self.deps_extractor.extract(nodes, raw_graph)

        try:
            graph_pointer = await self.graph_store.upsert_raw_clean(
                traj_meta={
                    "trajectory_id": trajectory_id,
                    "tenant_id": cmd.tenant_id,
                    "agent_id": cmd.agent_id,
                    "task_type": cmd.labels.get("task_type"),
                    "failure_signature": failure_signature,
                },
                raw=raw_graph,
                clean=clean_graph,
            )
        except Exception as e:
            await self.audit_logger.write(
                action="commit",
                actor=actor,
                tenant_id=cmd.tenant_id,
                target_uri=f"ctx://agent/{cmd.agent_id}/memories/trajectories/{trajectory_id}",
                result="error",
                metadata={"stage": "graph_store", "error": str(e)},
            )
            return CommitResult(
                trajectory_id=trajectory_id,
                nodes=len(raw_graph["nodes"]),
                edges=len(raw_graph["edges"]),
                status="accepted_with_retry",
                warnings=warnings + ["graph_store_failed"],
            )

        traj_uri = f"ctx://agent/{cmd.agent_id}/memories/trajectories/{trajectory_id}"
        await self.fs_repo.put_trajectory_bundle(
            {
                "trajectory_uri": traj_uri,
                "trajectory_json": {
                    "trajectory_id": trajectory_id,
                    "tenant_id": cmd.tenant_id,
                    "agent_id": cmd.agent_id,
                    "task_type": cmd.labels.get("task_type"),
                    "status": "success_or_partial",
                    "failure_signature": failure_signature,
                },
                "graph_pointer": graph_pointer,
                "raw_steps": norm_steps,
                "abstract_md": l0,
                "overview_md": l1,
                "deps_json": deps,
            }
        )

        event = {
            "type": "TrajectoryCommitted",
            "tenant_id": cmd.tenant_id,
            "trajectory_id": trajectory_id,
            "trajectory_uri": traj_uri,
            "task_type": cmd.labels.get("task_type"),
        }
        await self.event_log.append(event)
        await self.embedding_queue.enqueue(event)

        await self.audit_logger.write(
            action="commit",
            actor=actor,
            tenant_id=cmd.tenant_id,
            target_uri=traj_uri,
            result="success",
            metadata={"nodes": len(raw_graph["nodes"]), "edges": len(raw_graph["edges"])},
        )
        await self.idempotency_repo.put(
            idem_key,
            {"trajectory_id": trajectory_id, "nodes": len(raw_graph["nodes"]), "edges": len(raw_graph["edges"])},
        )
        return CommitResult(
            trajectory_id=trajectory_id,
            nodes=len(raw_graph["nodes"]),
            edges=len(raw_graph["edges"]),
            status="accepted",
            warnings=warnings,
        )
```

---

## 11.2 RetrieveOrchestrator（双路召回 + 融合）

```python
from dataclasses import dataclass
from typing import Any


@dataclass
class RetrieveCommand:
    tenant_id: str
    agent_id: str
    task_description: str
    partial_trajectory: list[dict[str, Any]] | None
    constraints: dict[str, Any]
    task_type: str | None
    top_k: int = 5
    include_stale: bool = False


class RetrieveOrchestrator:
    def __init__(
        self,
        acl_engine,
        query_parser,
        query_graph_builder,
        semantic_recall,
        graph_recall,
        reranker,
        evidence_builder,
        fs_repo,
        mask_engine,
        feedback_repo,
        audit_logger,
    ):
        self.acl = acl_engine
        self.query_parser = query_parser
        self.query_graph_builder = query_graph_builder
        self.semantic_recall = semantic_recall
        self.graph_recall = graph_recall
        self.reranker = reranker
        self.evidence_builder = evidence_builder
        self.fs_repo = fs_repo
        self.mask_engine = mask_engine
        self.feedback_repo = feedback_repo
        self.audit_logger = audit_logger

    async def handle(self, cmd: RetrieveCommand, actor: str) -> dict[str, Any]:
        await self.acl.assert_allowed(
            principal=actor,
            action="retrieve",
            resource=f"ctx://agent/{cmd.agent_id}/memories/trajectories/*",
        )

        q = self.query_parser.parse(
            task_description=cmd.task_description,
            constraints=cmd.constraints,
            task_type=cmd.task_type,
        )

        sem_candidates = await self.semantic_recall.search(
            tenant_id=cmd.tenant_id,
            query_text=q["embedding_text"],
            filters={
                "task_type": cmd.task_type,
                "tool_set": cmd.constraints.get("tool_whitelist"),
                "stale_flag": None if cmd.include_stale else False,
                "lifecycle_status": ["active", "cold"],
            },
            top_n=50,
        )

        graph_candidates: list[dict[str, Any]] = []
        if cmd.partial_trajectory:
            q_steps, _ = self.query_parser.normalize_partial(cmd.partial_trajectory)
            q_nodes = self.query_graph_builder.to_nodes(q_steps)
            q_graph = self.query_graph_builder.to_query_graph(q_nodes)
            graph_candidates = await self.graph_recall.search(
                tenant_id=cmd.tenant_id,
                query_graph=q_graph,
                top_n=50,
                include_raw_when_failure_clue=q["has_failure_clue"],
            )

        merged = self._union_candidates(sem_candidates, graph_candidates)
        fb_map = await self.feedback_repo.get_feedback_boost_map(
            tenant_id=cmd.tenant_id,
            trajectory_ids=[c["trajectory_id"] for c in merged],
        )
        ranked = self.reranker.hybrid_rank(
            candidates=merged,
            feedback_boost_map=fb_map,
            has_partial=bool(cmd.partial_trajectory),
        )[: cmd.top_k]

        items = []
        for c in ranked:
            meta = await self.fs_repo.get_trajectory_meta(cmd.tenant_id, c["trajectory_id"])
            overview = await self.fs_repo.get_overview(meta["overview_uri"])
            evidence = await self.evidence_builder.build(
                tenant_id=cmd.tenant_id,
                trajectory_id=c["trajectory_id"],
                graph_pointer=meta["graph_pointer"],
                semantic_hit=c.get("semantic_hit"),
                graph_hit=c.get("graph_hit"),
            )
            item = {
                "trajectory_id": c["trajectory_id"],
                "score": c["final_score"],
                "semantic_score": c.get("semantic_score"),
                "graph_score": c.get("graph_score"),
                "rationale": c["rationale"],
                "overview": overview,
                "evidence": evidence,
            }
            masked = await self.mask_engine.apply(
                principal=actor,
                action="retrieve",
                resource_uri=meta["trajectory_uri"],
                payload=item,
            )
            items.append(masked)

        await self.audit_logger.write(
            action="retrieve",
            actor=actor,
            tenant_id=cmd.tenant_id,
            target_uri=f"ctx://agent/{cmd.agent_id}/memories/trajectories/*",
            result="success",
            metadata={"query_hash": q["query_hash"], "returned": [x["trajectory_id"] for x in items], "top_k": cmd.top_k},
        )
        return {"items": items}

    @staticmethod
    def _union_candidates(sem_candidates, graph_candidates):
        merged = {}
        for s in sem_candidates:
            tid = s["trajectory_id"]
            merged[tid] = {
                "trajectory_id": tid,
                "semantic_score": s["score"],
                "semantic_hit": s,
                "graph_score": None,
                "graph_hit": None,
            }
        for g in graph_candidates:
            tid = g["trajectory_id"]
            if tid not in merged:
                merged[tid] = {
                    "trajectory_id": tid,
                    "semantic_score": 0.0,
                    "semantic_hit": None,
                    "graph_score": g["score"],
                    "graph_hit": g,
                }
            else:
                merged[tid]["graph_score"] = g["score"]
                merged[tid]["graph_hit"] = g
        return list(merged.values())
```

---

## 11.3 FeedbackOrchestrator（质量回写）

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class FeedbackCommand:
    tenant_id: str
    trajectory_id: str
    session_id: str
    outcome: str
    evidence: dict


class FeedbackOrchestrator:
    def __init__(self, acl_engine, feedback_repo, quality_updater, vector_store, graph_store, event_log, audit_logger):
        self.acl = acl_engine
        self.feedback_repo = feedback_repo
        self.quality_updater = quality_updater
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.event_log = event_log
        self.audit_logger = audit_logger

    async def handle(self, cmd: FeedbackCommand, actor: str) -> dict:
        if cmd.outcome not in {"adopted", "ignored", "corrected", "irrelevant"}:
            raise ValueError(f"invalid outcome: {cmd.outcome}")

        await self.acl.assert_allowed(
            principal=actor,
            action="feedback",
            resource=f"ctx://agent/*/memories/trajectories/{cmd.trajectory_id}",
        )

        fb = {
            "tenant_id": cmd.tenant_id,
            "trajectory_id": cmd.trajectory_id,
            "session_id": cmd.session_id,
            "outcome": cmd.outcome,
            "evidence": cmd.evidence,
            "created_at": datetime.utcnow().isoformat(),
        }
        await self.feedback_repo.append(fb)

        delta = self.quality_updater.compute_delta(cmd.outcome)
        quality_state = await self.quality_updater.apply(
            tenant_id=cmd.tenant_id,
            trajectory_id=cmd.trajectory_id,
            delta=delta,
            evidence=cmd.evidence,
        )

        await self.vector_store.update_filter_fields(
            tenant_id=cmd.tenant_id,
            trajectory_id=cmd.trajectory_id,
            fields={
                "quality_score": quality_state["trajectory_quality_score"],
                "adopted_count": quality_state["adopted_count"],
                "ignored_count": quality_state["ignored_count"],
                "corrected_count": quality_state["corrected_count"],
            },
        )
        await self.graph_store.update_reusability(
            tenant_id=cmd.tenant_id,
            trajectory_id=cmd.trajectory_id,
            outcome=cmd.outcome,
            evidence=cmd.evidence,
        )

        await self.event_log.append(
            {
                "type": "TrajectoryFeedbackReceived",
                "tenant_id": cmd.tenant_id,
                "trajectory_id": cmd.trajectory_id,
                "outcome": cmd.outcome,
            }
        )
        await self.audit_logger.write(
            action="feedback",
            actor=actor,
            tenant_id=cmd.tenant_id,
            target_uri=f"ctx://agent/*/memories/trajectories/{cmd.trajectory_id}",
            result="success",
            metadata={"outcome": cmd.outcome, "quality_score": quality_state["trajectory_quality_score"]},
        )
        return {"status": "ok", "quality_state": quality_state}
```

---

## 11.4 API 路由示意（FastAPI）

```python
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api/v1/amc", tags=["amc"])


@router.post("/commit")
async def commit_endpoint(req: CommitRequest, actor=Depends(current_actor), oc=Depends(commit_orchestrator)):
    result = await oc.handle(
        CommitCommand(
            tenant_id=req.tenant_id,
            agent_id=req.agent_id,
            session_id=req.session_id,
            task_id=req.task_id,
            trajectory=req.trajectory,
            labels=req.labels or {},
            is_incremental=req.is_incremental,
            trajectory_id=req.trajectory_id,
        ),
        actor=actor.id,
    )
    return result.__dict__


@router.post("/retrieve")
async def retrieve_endpoint(req: RetrieveRequest, actor=Depends(current_actor), orc=Depends(retrieve_orchestrator)):
    return await orc.handle(
        RetrieveCommand(
            tenant_id=req.tenant_id,
            agent_id=req.agent_id,
            task_description=req.query.task_description,
            partial_trajectory=req.query.partial_trajectory,
            constraints=req.query.constraints or {},
            task_type=req.query.task_type,
            top_k=req.top_k,
            include_stale=req.query.include_stale,
        ),
        actor=actor.id,
    )


@router.post("/feedback")
async def feedback_endpoint(req: FeedbackRequest, actor=Depends(current_actor), ofb=Depends(feedback_orchestrator)):
    return await ofb.handle(
        FeedbackCommand(
            tenant_id=req.tenant_id,
            trajectory_id=req.trajectory_id,
            session_id=req.session_id,
            outcome=req.outcome,
            evidence=req.evidence or {},
        ),
        actor=actor.id,
    )
```

---

若本文件与 `AMC_plan/02~09` 规范冲突，以规范文档为准。
