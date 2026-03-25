import json

from contexthub.propagation.base import PropagationAction, PropagationRule


class SkillVersionDepRule(PropagationRule):
    """处理 dep_type='skill_version' 的使用依赖。

    artifact 在创建时引用了某个 skill version。
    当该 skill 发布新版本时，根据是否 breaking 决定是否标记 stale。
    """

    async def evaluate(self, event, target) -> PropagationAction:
        if event.get("change_type") != "version_published":
            return PropagationAction(
                action="no_action",
                reason="skill_version 依赖只响应 version_published 事件",
            )

        metadata = event.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        is_breaking = metadata.get("is_breaking", False)
        new_version = int(event["new_version"])
        pinned_version = target.get("pinned_version")

        if pinned_version is None:
            return PropagationAction(
                action="no_action",
                reason="skill_version dependency 缺少 pinned_version，跳过传播",
            )

        if pinned_version >= new_version:
            return PropagationAction(
                action="no_action",
                reason=f"artifact 已基于 v{pinned_version} 生成，不受 v{new_version} 发布影响",
            )

        if is_breaking:
            return PropagationAction(
                action="mark_stale",
                reason=f"artifact 依赖的 Skill 从 v{pinned_version} 演进到 breaking v{new_version}",
            )
        return PropagationAction(
            action="notify",
            reason=f"artifact 依赖的 Skill 从 v{pinned_version} 更新到 non-breaking v{new_version}",
        )
