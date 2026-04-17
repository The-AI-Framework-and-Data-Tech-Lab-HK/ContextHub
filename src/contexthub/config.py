from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgres://"):
        return "postgresql://" + url.removeprefix("postgres://")
    return url


class Settings(BaseSettings):
    database_url: str = "postgresql://contexthub:contexthub@localhost:5432/contexthub"
    api_key: str = "changeme"
    embedding_model: str = "text-embedding-3-small"
    propagation_enabled: bool = True
    propagation_sweep_interval: int = 30    # 秒，周期补扫间隔
    propagation_lease_timeout: int = 300    # 秒，processing 超时后回收
    lifecycle_enabled: bool = True
    lifecycle_sweep_interval: int = 3600
    openai_api_key: str = ""
    embedding_dimensions: int = 1536
    rerank_strategy: str = "keyword"
    search_over_retrieve_factor: int = 3
    search_default_top_k: int = 10
    catalog_type: str = "mock"  # Carrier-specific: catalog connector type
    doc_store_root: str = "./data/docs"
    max_document_size_mb: int = 50
    max_token_per_node: int = 2000

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
    )

    @property
    def asyncpg_database_url(self) -> str:
        return _normalize_postgres_url(self.database_url)

    @property
    def sqlalchemy_database_url(self) -> str:
        url = _normalize_postgres_url(self.database_url)
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
        return url
