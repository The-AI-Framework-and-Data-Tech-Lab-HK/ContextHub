from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Coroutine


@dataclass
class PropagationAction:
    action: str          # mark_stale | auto_update | notify | advisory | no_action
    reason: str
    auto_update_fn: Callable[..., Coroutine] | None = None


class PropagationRule(ABC):
    @abstractmethod
    async def evaluate(
        self, event: dict[str, Any], target: dict[str, Any]
    ) -> PropagationAction:
        """评估一个变更事件对一个依赖目标的影响。"""
        ...
