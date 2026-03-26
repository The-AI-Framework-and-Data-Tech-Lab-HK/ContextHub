"""CatalogConnector ABC: generic data catalog connector interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TableSchema:
    database: str
    table: str
    ddl: str
    columns: list[dict]  # [{"name": "id", "type": "BIGINT", "comment": "主键"}, ...]
    comment: str | None = None


@dataclass
class TableStats:
    row_count: int | None = None
    size_bytes: int | None = None
    last_updated: datetime | None = None


@dataclass
class CatalogChange:
    database: str
    table: str
    change_type: str  # 'schema_changed' | 'table_created' | 'table_deleted'
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RelationshipInfo:
    """FK / join relationship between two tables."""

    from_database: str
    from_table: str
    from_column: str
    to_database: str
    to_table: str
    to_column: str
    join_type: str = "inner"


class CatalogConnector(ABC):
    """通用数据目录连接器接口。"""

    @abstractmethod
    async def list_databases(self) -> list[str]: ...

    @abstractmethod
    async def list_tables(self, database: str) -> list[str]: ...

    @abstractmethod
    async def get_table_schema(self, database: str, table: str) -> TableSchema: ...

    @abstractmethod
    async def get_table_stats(self, database: str, table: str) -> TableStats: ...

    @abstractmethod
    async def get_sample_data(
        self, database: str, table: str, limit: int = 5
    ) -> list[dict]: ...

    @abstractmethod
    async def detect_changes(self, since: datetime) -> list[CatalogChange]: ...

    @abstractmethod
    async def get_relationships(self) -> list[RelationshipInfo]: ...
