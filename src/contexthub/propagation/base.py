from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class PropagationAction:
    action: str          # mark_stale | notify | advisory | no_action
    reason: str


class PropagationRule(ABC):
    @abstractmethod
    async def evaluate(
        self, event: dict[str, Any], target: dict[str, Any]
    ) -> PropagationAction:
        """评估一个变更事件对一个依赖目标的影响。"""
        ...
