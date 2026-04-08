from dataclasses import dataclass


@dataclass
class AccessDecision:
    allowed: bool
    field_masks: list[str] | None
    reason: str
