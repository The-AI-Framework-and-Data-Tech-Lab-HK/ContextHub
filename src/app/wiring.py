"""Application wiring for Phase 1 MVP."""

from __future__ import annotations

from fastapi import FastAPI

from api.routes.commit import router as commit_router
from api.routes.replay import router as replay_router
from app.config import AppSettings, load_settings
from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.dataflow_llm import LLMDataflowExtractor
from core.commit.service import CommitService
from core.indexing.trajectory_vector_indexer import TrajectoryVectorIndexer
from core.commit.summary_llm import LLMTrajectorySummarizer
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.factory import build_graph_store_writer
from infra.storage.vector.factory import build_vector_store_adapter


def create_app(settings: AppSettings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    # Keep app factory side-effect free except for creating local adapters.
    app = FastAPI(title="AMC", version="0.1.0")

    repo = LocalFSTrajectoryRepository(root=cfg.storage.localfs_root)
    audit = JsonlAuditLogger(file_path=cfg.storage.audit_file_path)
    graph_store = build_graph_store_writer(cfg)
    vector_indexer = None
    if cfg.indexing_async_enabled and cfg.embedding_provider.lower() == "openai" and cfg.openai_api_key:
        vector_store = build_vector_store_adapter(cfg)
        if vector_store is not None and cfg.indexing_include_levels:
            vector_indexer = TrajectoryVectorIndexer(
                vector_store=vector_store,
                embedding_model=cfg.embedding_model,
                api_key=cfg.openai_api_key,
                embedder_base_url=cfg.model_endpoints.embedder_base_url or None,
                embedding_mode=cfg.embedding_mode,
                include_levels=tuple(int(x) for x in cfg.indexing_include_levels),
            )
    dataflow_extractor = None
    llm_summarizer = None
    if cfg.openai_api_key:
        llm_summary = LLMTrajectorySummarizer(
            api_key=cfg.openai_api_key,
            model=cfg.llm_model,
            base_url=cfg.model_endpoints.llm_base_url or None,
            temperature=0.0,
        )
        llm_summarizer = llm_summary.summarize
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
        llm_summarizer=llm_summarizer,
        reasoning_min_confidence=cfg.commit.reasoning_min_confidence,
    )
    orchestrator = CommitOrchestrator(
        commit_service=service,
        repo=repo,
        audit=audit,
        graph_store=graph_store,
        vector_indexer=vector_indexer,
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
