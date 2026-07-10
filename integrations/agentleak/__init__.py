"""AgentLeak Phase 5 adapters and offline policy helpers.

The stable Task 2 surface normalizes AgentLeak channel traces, compiles
``private_vault`` / ``allowed_set`` into offline policies, and builds flow
payload references. It does not run the real benchmark, AgentLeak detectors, LLM
judges, or runtime policy oracles.
"""

from integrations.agentleak.loader import (
    load_trace_jsonl,
    load_trace_jsonl_with_warnings,
    normalize_trace_record,
    normalize_trace_record_with_warnings,
)
from integrations.agentleak.flow_runtime import (
    AgentLeakEnforcementResult,
    AgentLeakFlowRuntime,
    PolicyBackedAgentLeakACL,
)
from integrations.agentleak.freeze import (
    collect_git_state,
    freeze_formal_run,
    load_freeze_bundle,
    record_realized_subset,
    verify_freeze,
)
from integrations.agentleak.mapping import (
    channel_to_boundary,
    event_to_flow_payload,
    policy_to_flow_items,
    policy_to_flow_payload,
)
from integrations.agentleak.policy_compiler import compile_policy
from integrations.agentleak.secondary_channels import (
    AgentLeakSecondaryLoadResult,
    C7ReproducibilityFinding,
    assess_c7_reproducibility,
    load_secondary_trace_json,
    load_secondary_trace_json_with_warnings,
    normalize_secondary_trace_record,
    normalize_secondary_trace_record_with_warnings,
    secondary_event_to_flow_payload,
)
from integrations.agentleak.systems import (
    AgentLeakSystem,
    AgentLeakSystemId,
    build_agentleak_system,
    build_system_manifest_entry,
    get_system_spec,
    list_system_specs,
)
from integrations.agentleak.trace_schema import (
    AgentLeakChannel,
    AgentLeakEventType,
    AgentLeakTraceEvent,
    CompiledAgentLeakPolicy,
)

__all__ = [
    "AgentLeakChannel",
    "AgentLeakEnforcementResult",
    "AgentLeakEventType",
    "AgentLeakFlowRuntime",
    "AgentLeakSecondaryLoadResult",
    "AgentLeakSystem",
    "AgentLeakSystemId",
    "AgentLeakTraceEvent",
    "C7ReproducibilityFinding",
    "CompiledAgentLeakPolicy",
    "PolicyBackedAgentLeakACL",
    "assess_c7_reproducibility",
    "build_agentleak_system",
    "build_system_manifest_entry",
    "channel_to_boundary",
    "collect_git_state",
    "compile_policy",
    "event_to_flow_payload",
    "freeze_formal_run",
    "load_freeze_bundle",
    "record_realized_subset",
    "verify_freeze",
    "get_system_spec",
    "list_system_specs",
    "load_secondary_trace_json",
    "load_secondary_trace_json_with_warnings",
    "load_trace_jsonl",
    "load_trace_jsonl_with_warnings",
    "normalize_secondary_trace_record",
    "normalize_secondary_trace_record_with_warnings",
    "normalize_trace_record",
    "normalize_trace_record_with_warnings",
    "policy_to_flow_items",
    "policy_to_flow_payload",
    "secondary_event_to_flow_payload",
]

