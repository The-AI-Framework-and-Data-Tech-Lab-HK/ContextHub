"""MockCatalogConnector: in-memory mock for MVP testing."""

from __future__ import annotations

from datetime import datetime, timezone

from contexthub.connectors.base import (
    CatalogChange,
    CatalogConnector,
    RelationshipInfo,
    TableSchema,
    TableStats,
)

_TABLES: dict[str, dict[str, dict]] = {
    "prod": {
        "users": {
            "ddl": (
                "CREATE TABLE users (\n"
                "  id BIGINT PRIMARY KEY,\n"
                "  name VARCHAR(100) NOT NULL,\n"
                "  email VARCHAR(200) UNIQUE NOT NULL,\n"
                "  created_at TIMESTAMP DEFAULT NOW()\n"
                ")"
            ),
            "columns": [
                {"name": "id", "type": "BIGINT", "comment": "用户ID"},
                {"name": "name", "type": "VARCHAR(100)", "comment": "用户名"},
                {"name": "email", "type": "VARCHAR(200)", "comment": "邮箱"},
                {"name": "created_at", "type": "TIMESTAMP", "comment": "注册时间"},
            ],
            "comment": "用户主表",
            "stats": {"row_count": 50000, "size_bytes": 8_000_000},
            "sample": [
                {"id": 1, "name": "Alice", "email": "alice@example.com", "created_at": "2024-01-01"},
                {"id": 2, "name": "Bob", "email": "bob@example.com", "created_at": "2024-01-02"},
                {"id": 3, "name": "Carol", "email": "carol@example.com", "created_at": "2024-01-03"},
            ],
        },
        "orders": {
            "ddl": (
                "CREATE TABLE orders (\n"
                "  id BIGINT PRIMARY KEY,\n"
                "  user_id BIGINT NOT NULL REFERENCES users(id),\n"
                "  status VARCHAR(20) DEFAULT 'pending',\n"
                "  total_amount DECIMAL(12,2),\n"
                "  created_at TIMESTAMP DEFAULT NOW()\n"
                ")"
            ),
            "columns": [
                {"name": "id", "type": "BIGINT", "comment": "订单ID"},
                {"name": "user_id", "type": "BIGINT", "comment": "用户ID(FK)"},
                {"name": "status", "type": "VARCHAR(20)", "comment": "订单状态"},
                {"name": "total_amount", "type": "DECIMAL(12,2)", "comment": "订单总额"},
                {"name": "created_at", "type": "TIMESTAMP", "comment": "下单时间"},
            ],
            "comment": "订单主表",
            "stats": {"row_count": 200000, "size_bytes": 40_000_000},
            "sample": [
                {"id": 101, "user_id": 1, "status": "completed", "total_amount": 299.99, "created_at": "2024-03-01"},
                {"id": 102, "user_id": 2, "status": "pending", "total_amount": 59.00, "created_at": "2024-03-02"},
                {"id": 103, "user_id": 1, "status": "shipped", "total_amount": 150.50, "created_at": "2024-03-03"},
            ],
        },
        "products": {
            "ddl": (
                "CREATE TABLE products (\n"
                "  id BIGINT PRIMARY KEY,\n"
                "  name VARCHAR(200) NOT NULL,\n"
                "  category VARCHAR(50),\n"
                "  price DECIMAL(10,2) NOT NULL,\n"
                "  stock INT DEFAULT 0\n"
                ")"
            ),
            "columns": [
                {"name": "id", "type": "BIGINT", "comment": "商品ID"},
                {"name": "name", "type": "VARCHAR(200)", "comment": "商品名称"},
                {"name": "category", "type": "VARCHAR(50)", "comment": "商品分类"},
                {"name": "price", "type": "DECIMAL(10,2)", "comment": "单价"},
                {"name": "stock", "type": "INT", "comment": "库存"},
            ],
            "comment": "商品表",
            "stats": {"row_count": 5000, "size_bytes": 1_200_000},
            "sample": [
                {"id": 1001, "name": "Laptop", "category": "electronics", "price": 999.99, "stock": 50},
                {"id": 1002, "name": "Mouse", "category": "electronics", "price": 29.99, "stock": 200},
                {"id": 1003, "name": "T-Shirt", "category": "clothing", "price": 19.99, "stock": 500},
            ],
        },
        "order_items": {
            "ddl": (
                "CREATE TABLE order_items (\n"
                "  id BIGINT PRIMARY KEY,\n"
                "  order_id BIGINT NOT NULL REFERENCES orders(id),\n"
                "  product_id BIGINT NOT NULL REFERENCES products(id),\n"
                "  quantity INT NOT NULL,\n"
                "  unit_price DECIMAL(10,2) NOT NULL\n"
                ")"
            ),
            "columns": [
                {"name": "id", "type": "BIGINT", "comment": "行项ID"},
                {"name": "order_id", "type": "BIGINT", "comment": "订单ID(FK)"},
                {"name": "product_id", "type": "BIGINT", "comment": "商品ID(FK)"},
                {"name": "quantity", "type": "INT", "comment": "数量"},
                {"name": "unit_price", "type": "DECIMAL(10,2)", "comment": "成交单价"},
            ],
            "comment": "订单明细表",
            "stats": {"row_count": 600000, "size_bytes": 80_000_000},
            "sample": [
                {"id": 1, "order_id": 101, "product_id": 1001, "quantity": 1, "unit_price": 999.99},
                {"id": 2, "order_id": 101, "product_id": 1002, "quantity": 2, "unit_price": 29.99},
                {"id": 3, "order_id": 102, "product_id": 1003, "quantity": 3, "unit_price": 19.99},
            ],
        },
        "payments": {
            "ddl": (
                "CREATE TABLE payments (\n"
                "  id BIGINT PRIMARY KEY,\n"
                "  order_id BIGINT NOT NULL REFERENCES orders(id),\n"
                "  method VARCHAR(30) NOT NULL,\n"
                "  amount DECIMAL(12,2) NOT NULL,\n"
                "  paid_at TIMESTAMP\n"
                ")"
            ),
            "columns": [
                {"name": "id", "type": "BIGINT", "comment": "支付ID"},
                {"name": "order_id", "type": "BIGINT", "comment": "订单ID(FK)"},
                {"name": "method", "type": "VARCHAR(30)", "comment": "支付方式"},
                {"name": "amount", "type": "DECIMAL(12,2)", "comment": "支付金额"},
                {"name": "paid_at", "type": "TIMESTAMP", "comment": "支付时间"},
            ],
            "comment": "支付记录表",
            "stats": {"row_count": 180000, "size_bytes": 25_000_000},
            "sample": [
                {"id": 1, "order_id": 101, "method": "credit_card", "amount": 299.99, "paid_at": "2024-03-01"},
                {"id": 2, "order_id": 102, "method": "alipay", "amount": 59.00, "paid_at": "2024-03-02"},
                {"id": 3, "order_id": 103, "method": "wechat", "amount": 150.50, "paid_at": "2024-03-03"},
            ],
        },
    },
}

_RELATIONSHIPS: list[dict] = [
    {"from_db": "prod", "from_table": "orders", "from_col": "user_id", "to_db": "prod", "to_table": "users", "to_col": "id"},
    {"from_db": "prod", "from_table": "order_items", "from_col": "order_id", "to_db": "prod", "to_table": "orders", "to_col": "id"},
    {"from_db": "prod", "from_table": "order_items", "from_col": "product_id", "to_db": "prod", "to_table": "products", "to_col": "id"},
    {"from_db": "prod", "from_table": "payments", "from_col": "order_id", "to_db": "prod", "to_table": "orders", "to_col": "id"},
]


class MockCatalogConnector(CatalogConnector):
    """In-memory mock catalog connector for MVP testing."""

    def __init__(self) -> None:
        self._injected_changes: list[CatalogChange] = []

    def inject_change(self, change: CatalogChange) -> None:
        """Inject a simulated schema change for testing."""
        self._injected_changes.append(change)

    async def list_databases(self) -> list[str]:
        return list(_TABLES.keys())

    async def list_tables(self, database: str) -> list[str]:
        db = _TABLES.get(database, {})
        return list(db.keys())

    async def get_table_schema(self, database: str, table: str) -> TableSchema:
        info = _TABLES[database][table]
        return TableSchema(
            database=database,
            table=table,
            ddl=info["ddl"],
            columns=info["columns"],
            comment=info.get("comment"),
        )

    async def get_table_stats(self, database: str, table: str) -> TableStats:
        info = _TABLES[database][table]
        s = info.get("stats", {})
        return TableStats(
            row_count=s.get("row_count"),
            size_bytes=s.get("size_bytes"),
            last_updated=datetime.now(timezone.utc),
        )

    async def get_sample_data(
        self, database: str, table: str, limit: int = 5
    ) -> list[dict]:
        info = _TABLES[database][table]
        return info.get("sample", [])[:limit]

    async def detect_changes(self, since: datetime) -> list[CatalogChange]:
        result = [c for c in self._injected_changes if c.detected_at >= since]
        return result

    async def get_relationships(self) -> list[RelationshipInfo]:
        return [
            RelationshipInfo(
                from_database=r["from_db"],
                from_table=r["from_table"],
                from_column=r["from_col"],
                to_database=r["to_db"],
                to_table=r["to_table"],
                to_column=r["to_col"],
            )
            for r in _RELATIONSHIPS
        ]
