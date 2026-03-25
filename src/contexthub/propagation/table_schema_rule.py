from contexthub.propagation.base import PropagationAction, PropagationRule


class TableSchemaRule(PropagationRule):
    """处理 dep_type='table_schema' 的使用依赖。

    依赖某张表的 schema。当表 schema 变更时，source-aware 刷新 dependent 的 L0/L1。
    """

    async def evaluate(self, event, target) -> PropagationAction:
        return PropagationAction(
            action="auto_update",
            reason="依赖表的 schema 已变更，需 source-aware 刷新 dependent 的 L0/L1",
        )
