from contexthub.propagation.base import PropagationRule
from contexthub.propagation.skill_dep_rule import SkillVersionDepRule
from contexthub.propagation.table_schema_rule import TableSchemaRule
from contexthub.propagation.derived_memory_rule import DerivedMemoryRule
from contexthub.propagation.subscription_notify_rule import SkillSubscriptionNotifyRule


class PropagationRuleRegistry:
    """按 dep_type 路由到具体的 PropagationRule。"""

    def __init__(self, dep_rules: dict[str, PropagationRule]):
        self._dep_rules = dep_rules
        self._subscription_rule = SkillSubscriptionNotifyRule()

    @classmethod
    def default(cls) -> "PropagationRuleRegistry":
        return cls(
            dep_rules={
                "skill_version": SkillVersionDepRule(),
                "table_schema": TableSchemaRule(),
                "derived_from": DerivedMemoryRule(),
            },
        )

    def get_dep_rule(self, dep_type: str) -> PropagationRule | None:
        return self._dep_rules.get(dep_type)

    @property
    def subscription_rule(self) -> PropagationRule:
        return self._subscription_rule
