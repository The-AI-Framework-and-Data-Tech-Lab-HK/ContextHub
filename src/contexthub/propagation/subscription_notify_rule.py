from contexthub.propagation.base import PropagationAction, PropagationRule


class SkillSubscriptionNotifyRule(PropagationRule):
    """处理 skill_subscriptions 中的订阅者。

    订阅者不是 artifact，没有可以过时的"内容"。
    传播对订阅者只做通知，不做 stale 标记。
    """

    async def evaluate(self, event, target) -> PropagationAction:
        new_ver = event.get("new_version", "?")
        pinned = target.get("pinned_version")

        if pinned is None:
            return PropagationAction(
                action="notify",
                reason=f"Skill 已更新到 v{new_ver}",
            )
        return PropagationAction(
            action="advisory",
            reason=f"Skill v{new_ver} 已发布，当前 pin 在 v{pinned}",
        )
