from contexthub.propagation.base import PropagationAction, PropagationRule


class DerivedMemoryRule(PropagationRule):
    """处理 dep_type='derived_from' 的使用依赖。

    从某个共享 memory 派生的私有 memory。
    当源 memory 被修改时，通知派生方。MVP 中仅日志。
    """

    async def evaluate(self, event, target) -> PropagationAction:
        change_type = event.get("change_type", "")
        if change_type == "modified":
            return PropagationAction(
                action="notify",
                reason="源 memory 已修改，派生方可能需要更新",
            )
        return PropagationAction(
            action="no_action",
            reason=f"源 memory 变更类型 {change_type} 不需要传播",
        )
