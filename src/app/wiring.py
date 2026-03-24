"""Application wiring for Phase 1 MVP."""

from __future__ import annotations

from fastapi import FastAPI

from api.routes.commit import router as commit_router
from api.routes.replay import router as replay_router
from app.config import AppSettings, load_settings
from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.dataflow_llm import LLMDataflowExtractor
from core.commit.service import CommitService
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository


def create_app(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    # Keep app factory side-effect free except for creating local adapters.
    app = FastAPI(title="AMC", version="0.1.0")

    repo = LocalFSTrajectoryRepository(root=cfg.storage.localfs_root)
    audit = JsonlAuditLogger(file_path=cfg.storage.audit_file_path)
    dataflow_extractor = None
    if cfg.commit.dataflow_extractor.lower() == "llm":
        if cfg.openai_api_key:
            llm_extractor = LLMDataflowExtractor(
                api_key=cfg.openai_api_key,
                model=cfg.llm_model,
                base_url=cfg.model_endpoints.llm_base_url or None,
                temperature=cfg.commit.dataflow_llm_temperature,
            )
            dataflow_extractor = llm_extractor.extract
        else:
            # Keep app runnable in dev; falls back to rule-based extraction when key missing.
            print("[AMC] dataflow_extractor=llm but AMC_OPENAI_API_KEY is empty, fallback to rule_based.")
    service = CommitService(
        max_action_result_chars=cfg.commit.max_action_result_chars,
        temporal_fallback_edge=cfg.commit.temporal_fallback_edge,
        dataflow_extractor=dataflow_extractor,
    )
    orchestrator = CommitOrchestrator(
        commit_service=service,
        repo=repo,
        audit=audit,
        idempotency_enabled=cfg.commit.idempotency_enabled,
    )

    # Store wired singletons in app.state for FastAPI dependencies.
    app.state.settings = cfg
    app.state.commit_orchestrator = orchestrator

    app.include_router(commit_router, prefix=cfg.api.prefix)
    app.include_router(replay_router, prefix=cfg.api.prefix)

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        # Liveness probe only; deeper checks can be added later.
        return {"status": "ok"}

    return app
