"""ContextHub-owned runtime S2 wrapper for EntCollabBench actions.

This module stays outside the external EntCollabBench checkout. It provides
small hooks that a harness can call before tool dispatch and after a runtime
result/timeout boundary, while keeping enforcement services and schema lookups
injectable for unit tests.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from contexthub.enforcement.decision import GuardrailDecision
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.enforcement.service import EnforcementService
from contexthub.services.acl_service import ACLService

from integrations.entcollabbench import closure_adapter, mapping, tool_contract_adapter
from integrations.entcollabbench.interceptor import EnforcementAction, EnforcementInterceptor
from integrations.entcollabbench.mcp_runtime_adapter import (
    McpEndpointConfig,
    ToolSchemaCache,
    normalize_tool_schema_record,
)
from integrations.entcollabbench.world_loader import LoadedWorld

SchemaProvider = Callable[[str, str], Mapping[str, Any]]
RoleChecker = Callable[[str, str], Awaitable[bool]]
ObjectExists = Callable[[str], Awaitable[bool]]
ProvenanceCheck = Callable[[str, str], Awaitable[bool]]
MutationIntentProvider = Callable[[str], str]


@dataclass(frozen=True)
class RuntimeToolEnforcementResult:
    """Decision and caller action for a pre-dispatch tool boundary."""

    decision: GuardrailDecision
    action: EnforcementAction
    contract: dict[str, Any]
    normalized_args: dict[str, Any]
    schema_source: str


@dataclass(frozen=True)
class RuntimeClosureEnforcementResult:
    """Decision, action, and closure diagnostics for a terminal boundary."""

    decision: GuardrailDecision
    action: EnforcementAction
    checklist: dict[str, Any]
    missing_actions: list[str]
    open_questions: list[str]


@dataclass(frozen=True)
class RuntimeHandoffEnforcementResult:
    """Thin handoff boundary result for callers that already have a packet."""

    decision: GuardrailDecision
    action: EnforcementAction
    packet: dict[str, Any]
    limitation: str = (
        "EntCollabBench structured handoff packets are not yet intercepted by "
        "this wrapper; callers must provide a packet captured from their own harness."
    )


class NoopStaleness:
    async def any_stale_or_blocked_refs(self, db, refs):
        return []


class NullRepo:
    @asynccontextmanager
    async def session(self, account_id: str) -> AsyncIterator[None]:
        yield None


class ContextHubRuntimeWrapper:
    """Runtime-facing S2 gates owned by ContextHub, not EntCollabBench."""

    def __init__(
        self,
        *,
        repo: Any | None = None,
        account_id: str = "entcollab-runtime",
        loaded: LoadedWorld | None = None,
        service: EnforcementService | None = None,
        schema_provider: SchemaProvider | None = None,
        endpoint_config: McpEndpointConfig | None = None,
        schema_cache: ToolSchemaCache | None = None,
        role_checker: RoleChecker | None = None,
        object_exists: ObjectExists | None = None,
        provenance_check: ProvenanceCheck | None = None,
        staleness: Any | None = None,
        mutation_intent_provider: MutationIntentProvider | None = None,
    ):
        self._schema_provider = schema_provider
        self._endpoint_config = endpoint_config
        self._schema_cache = schema_cache or (
            ToolSchemaCache(endpoint_config) if endpoint_config is not None else None
        )
        self._mutation_intent_provider = (
            mutation_intent_provider
            if mutation_intent_provider is not None
            else infer_mutation_intent
        )
        self._staleness = staleness if staleness is not None else NoopStaleness()
        self._loaded = loaded if loaded is not None else LoadedWorld()
        self._interceptor = EnforcementInterceptor(
            repo if repo is not None else NullRepo(),
            account_id,
            self._loaded,
            guardrails=None,
            service=service
            or self._default_service(
                role_checker,
                object_exists=object_exists,
                provenance_check=provenance_check,
            ),
        )

    async def enforce_tool_call_before_execute(
        self,
        *,
        agent_id: str,
        server: str,
        tool_name: str,
        raw_args: Mapping[str, Any] | None = None,
        schema_record: Mapping[str, Any] | None = None,
        schema_provider: SchemaProvider | None = None,
        required_role: str | None = None,
        mutation_intent: str | None = None,
    ) -> RuntimeToolEnforcementResult:
        """Gate a tool call before the external runtime executes it."""

        schema, schema_source = self._schema_for_tool(
            server,
            tool_name,
            schema_record=schema_record,
            schema_provider=schema_provider,
        )
        normalized_args = tool_contract_adapter.normalize_tool_args(server, tool_name, raw_args)
        contract = tool_contract_adapter.tool_schema_to_contract_fields(
            server,
            schema,
            required_role=agent_id if required_role is None else required_role,
            mutation_intent=mutation_intent
            if mutation_intent is not None
            else self._mutation_intent_provider(tool_name),
        )
        contract["arg_schema"] = guardrail_compatible_schema(contract["arg_schema"])

        decision = await self._interceptor.on_tool_call(agent_id, contract, normalized_args)
        action = self._interceptor.apply(decision)
        return RuntimeToolEnforcementResult(
            decision=decision,
            action=action,
            contract=contract,
            normalized_args=normalized_args,
            schema_source=schema_source,
        )

    async def enforce_closure_after_result_or_timeout(
        self,
        *,
        agent_id: str,
        workflow_id: str,
        ground_truth: list[dict[str, Any]] | dict[str, Any],
        trace_events: list[dict[str, Any]] | dict[str, Any],
        runtime_summary: Mapping[str, Any] | None = None,
        declared_context_uris: list[str] | None = None,
        require_decision: bool = False,
        decision_label: str | None = None,
        rule_citations: list[str] | None = None,
    ) -> RuntimeClosureEnforcementResult:
        """Gate closure after a runtime result, timeout, or partial trace boundary."""

        checklist = closure_adapter.build_workflow_closure_payload(
            workflow_id=workflow_id,
            ground_truth=ground_truth,
            trace_events=trace_events,
            runtime_summary=dict(runtime_summary or {}),
            require_decision=require_decision,
            decision_label=decision_label,
            rule_citations=rule_citations,
        )
        decision = await self._interceptor.on_closure(
            agent_id,
            checklist,
            declared_context_uris or [],
            workflow_id,
        )
        action = self._interceptor.apply(decision)
        diagnostics = checklist.get("diagnostics") or {}
        return RuntimeClosureEnforcementResult(
            decision=decision,
            action=action,
            checklist=checklist,
            missing_actions=list(diagnostics.get("missing_actions") or []),
            open_questions=list(checklist.get("open_questions") or []),
        )

    async def enforce_handoff_before_delegate(
        self,
        *,
        sender: str,
        recipient: str,
        packet: Mapping[str, Any],
    ) -> RuntimeHandoffEnforcementResult:
        """Thin handoff hook for harnesses that already expose a structured packet."""

        packet_dict = dict(packet)
        decision = await self._interceptor.on_handoff(sender, recipient, packet_dict)
        action = self._interceptor.apply(decision)
        return RuntimeHandoffEnforcementResult(
            decision=decision,
            action=action,
            packet=packet_dict,
        )

    def _default_service(
        self,
        role_checker: RoleChecker | None,
        *,
        object_exists: ObjectExists | None = None,
        provenance_check: ProvenanceCheck | None = None,
    ) -> EnforcementService:
        return EnforcementService(
            [
                HandoffGuardrail(
                    ACLService(),
                    self._staleness,
                    object_uri_resolver=self._loaded.object_uri,
                    version_uri_resolver=mapping.resolve_version_tag,
                ),
                ToolStateGuardrail(
                    self._staleness,
                    role_checker=role_checker or exact_role_checker,
                    object_exists=object_exists,
                    provenance_check=provenance_check,
                ),
                ClosureGuardrail(self._staleness),
            ],
            audit=None,
        )

    def _schema_for_tool(
        self,
        server: str,
        tool_name: str,
        *,
        schema_record: Mapping[str, Any] | None,
        schema_provider: SchemaProvider | None,
    ) -> tuple[dict[str, Any], str]:
        if schema_record is not None:
            return (
                normalize_tool_schema_record(schema_record, tool_name=tool_name),
                "provided-schema-record",
            )

        provider = schema_provider or self._schema_provider
        if provider is not None:
            return (
                normalize_tool_schema_record(provider(server, tool_name), tool_name=tool_name),
                "injected-schema-provider",
            )

        if self._schema_cache is not None:
            return self._schema_cache.get(server, tool_name)

        return (
            normalize_tool_schema_record({"name": tool_name}, tool_name=tool_name),
            "schema-unavailable:no-provider",
        )


async def exact_role_checker(agent_id: str, required_role: str) -> bool:
    return str(agent_id or "").strip() == str(required_role or "").strip()


def build_full_s2_runtime_wrapper(
    *,
    repo: Any,
    loaded: LoadedWorld,
    account_id: str = "entcollab-runtime",
    schema_provider: SchemaProvider | None = None,
    endpoint_config: McpEndpointConfig | None = None,
    schema_cache: ToolSchemaCache | None = None,
    mutation_intent_provider: MutationIntentProvider | None = None,
) -> ContextHubRuntimeWrapper:
    """Construct the DB-backed S2 runtime wrapper used by online proxies.

    Audit is deliberately not wired into ``EnforcementService`` here; proxy
    callers persist audit records through a side channel after the gate decides.
    """

    from integrations.entcollabbench.systems import (
        _object_exists,
        _provenance_check,
        _role_checker,
    )

    return ContextHubRuntimeWrapper(
        repo=repo,
        account_id=account_id,
        loaded=loaded,
        schema_provider=schema_provider,
        endpoint_config=endpoint_config,
        schema_cache=schema_cache,
        role_checker=_role_checker(loaded),
        object_exists=_object_exists(loaded),
        provenance_check=_provenance_check(loaded),
        staleness=StalenessChecker(),
        mutation_intent_provider=mutation_intent_provider,
    )


def build_s1_runtime_wrapper(
    *,
    account_id: str = "entcollab-runtime",
    schema_provider: SchemaProvider | None = None,
    endpoint_config: McpEndpointConfig | None = None,
    schema_cache: ToolSchemaCache | None = None,
    mutation_intent_provider: MutationIntentProvider | None = None,
) -> ContextHubRuntimeWrapper:
    """Construct the online S1 generic guardrail runtime wrapper.

    S1 is the H3 control: it intentionally uses no ContextHub repo, loaded world,
    ACL, provenance, or staleness state. It only validates generic boundary shape
    and live/static schema details already present in the request contract.
    """

    from integrations.entcollabbench.systems import GenericGuardrail

    return ContextHubRuntimeWrapper(
        repo=NullRepo(),
        account_id=account_id,
        service=EnforcementService([GenericGuardrail()], audit=None),
        schema_provider=schema_provider,
        endpoint_config=endpoint_config,
        schema_cache=schema_cache,
        mutation_intent_provider=mutation_intent_provider,
    )


def infer_mutation_intent(tool_name: str) -> str:
    name = str(tool_name or "").lower()
    if name.startswith(("update", "set", "close", "resolve")):
        return "update"
    if name.startswith(("create", "send", "post", "add")):
        return "create"
    return ""


def guardrail_compatible_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Convert JSON Schema details to the subset ToolStateGuardrail accepts."""

    normalized = dict(schema)
    properties = {}
    for name, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, Mapping):
            properties[name] = spec
            continue
        item = dict(spec)
        schema_type = item.get("type")
        if isinstance(schema_type, list):
            first_supported = next(
                (
                    value
                    for value in schema_type
                    if value in {"string", "integer", "number", "boolean", "array", "object"}
                ),
                None,
            )
            if first_supported is None:
                item.pop("type", None)
            else:
                item["type"] = first_supported
        properties[name] = item
    normalized["properties"] = properties
    return normalized
