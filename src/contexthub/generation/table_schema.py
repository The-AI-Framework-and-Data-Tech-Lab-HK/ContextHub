"""TableSchemaGenerator: template-based L0/L1 generation for datalake tables."""

from __future__ import annotations

from contexthub.connectors.base import TableSchema
from contexthub.generation.base import GeneratedContent

_TRUNCATE_L0 = 80
_TRUNCATE_L1 = 500


class TableSchemaGenerator:
    """为数据湖表生成 L0 和 L1 内容。MVP 阶段不依赖 LLM，使用模板化生成。"""

    def generate_from_schema(self, schema: TableSchema) -> GeneratedContent:
        l0 = self._build_l0(schema)
        l1 = self._build_l1(schema)
        return GeneratedContent(l0=l0, l1=l1)

    def _build_l0(self, schema: TableSchema) -> str:
        desc = schema.comment or self._infer_description(schema)
        raw = f"{schema.table} 表 - {desc}"
        return raw[:_TRUNCATE_L0]

    def _build_l1(self, schema: TableSchema) -> str:
        lines = [
            f"## {schema.database}.{schema.table}",
            "",
            f"{schema.comment or self._infer_description(schema)}",
            "",
            "| 字段 | 类型 | 说明 |",
            "|---|---|---|",
        ]
        for col in schema.columns:
            lines.append(f"| {col['name']} | {col['type']} | {col.get('comment', '')} |")
        return "\n".join(lines)[:_TRUNCATE_L1]

    @staticmethod
    def _infer_description(schema: TableSchema) -> str:
        col_names = [c["name"] for c in schema.columns]
        if "order_id" in col_names and "product_id" in col_names:
            return "订单明细"
        if "order_id" in col_names and "amount" in col_names:
            return "支付记录"
        if "user_id" in col_names and "total_amount" in col_names:
            return "订单数据"
        if "email" in col_names:
            return "用户信息"
        if "price" in col_names:
            return "商品信息"
        return f"{schema.table} 数据表"
