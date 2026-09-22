from contexthub.propagation.base import PropagationAction, PropagationRule


class TableSchemaRule(PropagationRule):
    """处理 dep_type='table_schema' 的使用依赖。

    依赖某张表的 schema。表 schema 变更时把 dependent 标记过期。

    这里不重写 dependent 的内容：改写内容需要真正的语义重算生成器和
    输出有效性契约，本仓库都没有。标记过期是诚实的（读取时被屏蔽），
    而把旧内容盖章成 fresh 不是。
    """

    async def evaluate(self, event, target) -> PropagationAction:
        return PropagationAction(
            action="mark_stale",
            reason="依赖表的 schema 已变更，dependent 内容可能不再成立",
        )
