"""App configuration loader for AMC Phase 1."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class AppSection(BaseModel):
    env: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"


class ApiSection(BaseModel):
    prefix: str = "/api/v1/amc"
    max_payload_mb: int = 20


class CommitSection(BaseModel):
    idempotency_enabled: bool = True
    temporal_fallback_edge: bool = True
    dataflow_extractor: str = "rule_based"  # rule_based | llm
    dataflow_llm_temperature: float = 0.0
    max_action_result_chars: int = 12000


class StorageSection(BaseModel):
    localfs_root: str = "./data/content"
    event_jsonl_path: str = "./data/events/amc_events.jsonl"
    audit_file_path: str = "./data/audit/amc_audit.log"


class ModelEndpointsSection(BaseModel):
    embedder_base_url: str = ""
    llm_base_url: str = ""


class AppSettings(BaseModel):
    # Structured sections loaded from config.yaml
    app: AppSection = Field(default_factory=AppSection)
    api: ApiSection = Field(default_factory=ApiSection)
    commit: CommitSection = Field(default_factory=CommitSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    model_endpoints: ModelEndpointsSection = Field(default_factory=ModelEndpointsSection)

    # Flat env-driven fields (kept top-level for easier access in wiring/adapters)
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    llm_model: str = "gpt-4.1-mini"
    openai_api_key: str = ""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def load_settings(config_path: str | None = None) -> AppSettings:
    # 1) Load .env first so env vars are visible to os.getenv.
    load_dotenv()
    cfg_path = Path(config_path or os.getenv("AMC_CONFIG_PATH", "config/config.yaml"))
    # 2) Parse YAML defaults.
    raw = _read_yaml(cfg_path)

    app_raw = raw.get("app") or {}
    api_raw = raw.get("api") or {}
    storage_raw = raw.get("storage") or {}
    commit_raw = raw.get("commit") or {}
    idempotency_raw = (commit_raw.get("idempotency") or {}) if isinstance(commit_raw, dict) else {}
    graph_raw = (commit_raw.get("graph") or {}) if isinstance(commit_raw, dict) else {}
    normalize_raw = (commit_raw.get("normalize") or {}) if isinstance(commit_raw, dict) else {}
    event_raw = (storage_raw.get("event_log") or {}) if isinstance(storage_raw, dict) else {}
    content_raw = (storage_raw.get("content_store") or {}) if isinstance(storage_raw, dict) else {}
    audit_raw = raw.get("audit") or {}
    model_raw = raw.get("model_endpoints") or {}

    settings = AppSettings(
        app=AppSection(
            env=str(app_raw.get("env", "dev")),
            host=str(app_raw.get("host", "0.0.0.0")),
            port=int(app_raw.get("port", 8000)),
            log_level=str(app_raw.get("log_level", "INFO")),
        ),
        api=ApiSection(
            prefix=str(api_raw.get("prefix", "/api/v1/amc")),
            max_payload_mb=int(api_raw.get("max_payload_mb", 20)),
        ),
        commit=CommitSection(
            idempotency_enabled=bool(idempotency_raw.get("enabled", True)),
            temporal_fallback_edge=bool(graph_raw.get("temporal_fallback_edge", True)),
            dataflow_extractor=str(graph_raw.get("dataflow_extractor", "rule_based")),
            dataflow_llm_temperature=float(graph_raw.get("llm_temperature", 0.0)),
            max_action_result_chars=int(normalize_raw.get("max_action_result_chars", 12000)),
        ),
        storage=StorageSection(
            localfs_root=str(content_raw.get("localfs_root", "./data/content")),
            event_jsonl_path=str(event_raw.get("jsonl_path", "./data/events/amc_events.jsonl")),
            audit_file_path=str(audit_raw.get("file_path", "./data/audit/amc_audit.log")),
        ),
        model_endpoints=ModelEndpointsSection(
            embedder_base_url=str(model_raw.get("embedder_base_url", "")),
            llm_base_url=str(model_raw.get("llm_base_url", "")),
        ),
    )

    # 3) Apply environment overrides (env > yaml).
    settings.app.env = os.getenv("AMC_ENV", settings.app.env)
    settings.app.log_level = os.getenv("AMC_LOG_LEVEL", settings.app.log_level)
    settings.storage.localfs_root = os.getenv("AMC_CONTENT_LOCAL_ROOT", settings.storage.localfs_root)
    settings.storage.event_jsonl_path = os.getenv(
        "AMC_EVENT_JSONL_PATH", settings.storage.event_jsonl_path
    )
    settings.storage.audit_file_path = os.getenv("AMC_AUDIT_FILE", settings.storage.audit_file_path)

    settings.model_endpoints.embedder_base_url = os.getenv(
        "AMC_EMBEDDING_BASE_URL", settings.model_endpoints.embedder_base_url
    )
    settings.model_endpoints.llm_base_url = os.getenv(
        "AMC_LLM_BASE_URL", settings.model_endpoints.llm_base_url
    )
    settings.commit.idempotency_enabled = os.getenv(
        "AMC_COMMIT_IDEMPOTENCY_ENABLED", str(settings.commit.idempotency_enabled)
    ).lower() in {"1", "true", "yes", "on"}
    settings.commit.temporal_fallback_edge = os.getenv(
        "AMC_COMMIT_TEMPORAL_FALLBACK_EDGE", str(settings.commit.temporal_fallback_edge)
    ).lower() in {"1", "true", "yes", "on"}
    settings.commit.dataflow_extractor = os.getenv(
        "AMC_COMMIT_DATAFLOW_EXTRACTOR",
        settings.commit.dataflow_extractor,
    ).strip()
    settings.commit.dataflow_llm_temperature = float(
        os.getenv(
            "AMC_COMMIT_DATAFLOW_LLM_TEMPERATURE",
            str(settings.commit.dataflow_llm_temperature),
        )
    )

    # 4) External service credentials/endpoints.
    settings.neo4j_uri = os.getenv("AMC_NEO4J_URI", settings.neo4j_uri)
    settings.neo4j_user = os.getenv("AMC_NEO4J_USER", settings.neo4j_user)
    settings.neo4j_password = os.getenv("AMC_NEO4J_PASSWORD", settings.neo4j_password)
    settings.neo4j_database = os.getenv("AMC_NEO4J_DATABASE", settings.neo4j_database)
    settings.embedding_provider = os.getenv("AMC_EMBEDDING_PROVIDER", settings.embedding_provider)
    settings.embedding_model = os.getenv("AMC_EMBEDDING_MODEL", settings.embedding_model)
    # Unified LLM model shared across LLM-powered features.
    settings.llm_model = os.getenv("AMC_LLM_MODEL", str(model_raw.get("llm_model", settings.llm_model))).strip()
    settings.openai_api_key = os.getenv("AMC_OPENAI_API_KEY", settings.openai_api_key)
    return settings
