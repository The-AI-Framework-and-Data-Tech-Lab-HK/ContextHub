"""System registry and baseline adapters for AgentLeak Phase 5.

AL-S1 and AL-S2 are baselines, not ContextHub flow implementations. AL-S1 may
use the offline compiled policy only to model store read-path visibility before
initial injection. AL-S2 is deliberately policy-blind and only applies generic
redaction rules to channel payload content.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable

from integrations.agentleak.generic_redaction import GenericRedactor, RedactionResult
from integrations.agentleak.trace_schema import AgentLeakTraceEvent, CompiledAgentLeakPolicy


class AgentLeakSystemId(StrEnum):
    AL_S0 = "AL-S0"
    AL_S1 = "AL-S1"
    AL_S2 = "AL-S2"
    AL_S3 = "AL-S3"
    AL_S3_REPAIR = "AL-S3-repair"


_AL_S3_SYSTEMS = frozenset({AgentLeakSystemId.AL_S3, AgentLeakSystemId.AL_S3_REPAIR})


@dataclass(frozen=True)
class AgentLeakSystemSpec:
    id: AgentLeakSystemId
    label: str
    description: str
    uses_context_hub_flow: bool
    uses_allowed_set_runtime: bool
    uses_online_llm_policy_oracle: bool = False
    runtime_guardrail: str = "none"
    comparable_with_al_s3: bool = True
    notes: tuple[str, ...] = ()

    def to_manifest_json(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "label": self.label,
            "description": self.description,
            "uses_context_hub_flow": self.uses_context_hub_flow,
            "uses_allowed_set_runtime": self.uses_allowed_set_runtime,
            "uses_online_llm_policy_oracle": self.uses_online_llm_policy_oracle,
            "runtime_guardrail": self.runtime_guardrail,
            "comparable_with_al_s3": self.comparable_with_al_s3,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class AgentLeakSystemResult:
    original_event: AgentLeakTraceEvent
    sanitized_event: AgentLeakTraceEvent | None
    forwarded: bool
    system_id: AgentLeakSystemId
    decision: dict[str, Any] = field(default_factory=dict)
    redaction: RedactionResult | None = None


SYSTEM_REGISTRY: dict[AgentLeakSystemId, AgentLeakSystemSpec] = {
    AgentLeakSystemId.AL_S0: AgentLeakSystemSpec(
        id=AgentLeakSystemId.AL_S0,
        label="no ContextHub",
        description="Native AgentLeak execution; no ACL, masking, or flow enforcement.",
        uses_context_hub_flow=False,
        uses_allowed_set_runtime=False,
        runtime_guardrail="identity",
    ),
    AgentLeakSystemId.AL_S1: AgentLeakSystemSpec(
        id=AgentLeakSystemId.AL_S1,
        label="store ACL only",
        description=(
            "Models context-store read-path ACL before initial injection only; "
            "does not inspect post-injection channel events."
        ),
        uses_context_hub_flow=False,
        uses_allowed_set_runtime=True,
        runtime_guardrail="store_acl_initial_injection_only",
        notes=(
            "May use CompiledAgentLeakPolicy.allowed_fields for initial vault visibility.",
            "Does not use recipient-aware flow payloads or channel-level ACL.",
        ),
    ),
    AgentLeakSystemId.AL_S2: AgentLeakSystemSpec(
        id=AgentLeakSystemId.AL_S2,
        label="generic redaction",
        description=(
            "Policy-blind canary/PII redaction baseline applied to channel payloads."
        ),
        uses_context_hub_flow=False,
        uses_allowed_set_runtime=False,
        runtime_guardrail="generic_redaction",
        notes=(
            "Does not read AgentLeak allowed_set, private_vault field policy, or provenance.",
            "Does not call an LLM judge or AgentLeak detector at runtime.",
        ),
    ),
    AgentLeakSystemId.AL_S3: AgentLeakSystemSpec(
        id=AgentLeakSystemId.AL_S3,
        label="ContextHub flow",
        description=(
            "Ownership-aware ContextHub flow runtime; implementation is provided by Task 3."
        ),
        uses_context_hub_flow=True,
        uses_allowed_set_runtime=True,
        runtime_guardrail="contexthub_flow_runtime",
        notes=("Task 4 registers this system but does not implement FlowGuardrail.",),
    ),
    AgentLeakSystemId.AL_S3_REPAIR: AgentLeakSystemSpec(
        id=AgentLeakSystemId.AL_S3_REPAIR,
        label="ContextHub flow (repair)",
        description=(
            "Ownership-aware ContextHub flow runtime in repair mode: field-level "
            "text redaction of unauthorized field values, forwarding the message "
            "instead of whole-message block."
        ),
        uses_context_hub_flow=True,
        uses_allowed_set_runtime=True,
        runtime_guardrail="contexthub_flow_runtime_repair",
        notes=(
            "Removes unauthorized field VALUES from channel content; keeps authorized content.",
            "Requires a Task 3 flow_runtime constructed with repair_mode=True.",
        ),
    ),
}


class AgentLeakSystem:
    """Uniform wrapper for AgentLeak Phase 5 system conditions."""

    def __init__(
        self,
        system_id: AgentLeakSystemId | str,
        *,
        redactor: GenericRedactor | None = None,
        flow_runtime: Any = None,
    ) -> None:
        self.system_id = AgentLeakSystemId(system_id)
        self.spec = SYSTEM_REGISTRY[self.system_id]
        self._redactor = redactor or GenericRedactor()
        self._flow_runtime = flow_runtime

    def filter_initial_vault(
        self,
        vault: dict[str, Any],
        *,
        policy: CompiledAgentLeakPolicy | None = None,
    ) -> dict[str, Any]:
        """Return fields visible during initial context-store injection.

        Only AL-S1 models store ACL here. Channel events after injection are not
        filtered by this method and must remain observable to AgentLeak's
        post-hoc detector.
        """

        source = dict(vault)
        if self.system_id != AgentLeakSystemId.AL_S1:
            return source
        if policy is None:
            return {}
        return {field: source[field] for field in policy.allowed_fields if field in source}

    def apply_event(self, event: AgentLeakTraceEvent) -> AgentLeakSystemResult:
        """Apply this system condition to a normalized channel event."""

        if self.system_id in {AgentLeakSystemId.AL_S0, AgentLeakSystemId.AL_S1}:
            sanitized = _copy_event_with_system(event, self.system_id)
            return AgentLeakSystemResult(
                original_event=event,
                sanitized_event=sanitized,
                forwarded=True,
                system_id=self.system_id,
                decision={
                    "verdict": "allow",
                    "guardrail": self.spec.runtime_guardrail,
                    "uses_online_llm_policy_oracle": False,
                },
            )

        if self.system_id == AgentLeakSystemId.AL_S2:
            redaction = self._redactor.redact(event.content)
            sanitized = replace(
                event,
                content=redaction.content,
                system=self.system_id.value,
                metadata={
                    **copy.deepcopy(event.metadata),
                    "generic_redaction": redaction.summary(),
                },
            )
            return AgentLeakSystemResult(
                original_event=event,
                sanitized_event=sanitized,
                forwarded=True,
                system_id=self.system_id,
                redaction=redaction,
                decision={
                    "verdict": "repair" if redaction.redacted else "allow",
                    "guardrail": self.spec.runtime_guardrail,
                    "masked_fields": [],
                    "redaction_patterns": redaction.summary()["patterns"],
                    "over_redaction": bool(redaction.over_redaction_candidates),
                    "uses_allowed_set": False,
                    "uses_online_llm_policy_oracle": False,
                },
            )

        if self.system_id in _AL_S3_SYSTEMS:
            if self._flow_runtime is None:
                raise RuntimeError("AL-S3 requires Task 3 flow_runtime; Task 4 only registers it")
            apply = getattr(self._flow_runtime, "enforce_event", None)
            if apply is None:
                raise TypeError("flow_runtime must provide enforce_event(event)")
            result = apply(event)
            if hasattr(result, "__await__"):
                raise TypeError("use apply_event_async for async AL-S3 runtimes")
            return result

        raise ValueError(f"unsupported AgentLeak system: {self.system_id}")

    async def apply_event_async(self, event: AgentLeakTraceEvent) -> Any:
        if self.system_id not in _AL_S3_SYSTEMS:
            return self.apply_event(event)
        if self._flow_runtime is None:
            raise RuntimeError("AL-S3 requires Task 3 flow_runtime; Task 4 only registers it")
        result = self._flow_runtime.enforce_event(event)
        if hasattr(result, "__await__"):
            return await result
        return result


def build_agentleak_system(
    system_id: AgentLeakSystemId | str,
    *,
    redactor: GenericRedactor | None = None,
    flow_runtime: Any = None,
) -> AgentLeakSystem:
    return AgentLeakSystem(system_id, redactor=redactor, flow_runtime=flow_runtime)


def get_system_spec(system_id: AgentLeakSystemId | str) -> AgentLeakSystemSpec:
    return SYSTEM_REGISTRY[AgentLeakSystemId(system_id)]


def list_system_specs() -> list[AgentLeakSystemSpec]:
    return [SYSTEM_REGISTRY[system_id] for system_id in AgentLeakSystemId]


def build_system_manifest_entry(system_id: AgentLeakSystemId | str) -> dict[str, Any]:
    return get_system_spec(system_id).to_manifest_json()


def _copy_event_with_system(
    event: AgentLeakTraceEvent,
    system_id: AgentLeakSystemId,
) -> AgentLeakTraceEvent:
    return replace(event, content=copy.deepcopy(event.content), system=system_id.value)


__all__ = [
    "AgentLeakSystem",
    "AgentLeakSystemId",
    "AgentLeakSystemResult",
    "AgentLeakSystemSpec",
    "SYSTEM_REGISTRY",
    "build_agentleak_system",
    "build_system_manifest_entry",
    "get_system_spec",
    "list_system_specs",
]
