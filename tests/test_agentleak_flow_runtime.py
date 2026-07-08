import json

import pytest

from contexthub.enforcement.decision import Verdict
from integrations.agentleak.flow_runtime import AgentLeakFlowRuntime
from integrations.agentleak.mapping import policy_to_flow_items
from integrations.agentleak.policy_compiler import compile_policy
from integrations.agentleak.trace_schema import AgentLeakChannel, AgentLeakTraceEvent


def _scenario(*, forbidden_action: str = "deny"):
    scenario = {
        "scenario_id": "scenario-1",
        "private_vault": {
            "records": [
                {
                    "record_id": "patient-1",
                    "fields": {
                        "name": "Ada Lovelace",
                        "ssn": "123-45-6789",
                    },
                }
            ]
        },
        "allowed_set": {
            "fields": ["name"],
            "forbidden_fields": ["ssn"],
        },
    }
    return compile_policy(scenario, forbidden_action=forbidden_action)


def _event(policy, **overrides):
    defaults = {
        "trace_id": "trace-1",
        "scenario_id": policy.scenario_id,
        "channel": AgentLeakChannel.C2,
        "actor": "agent-a",
        "recipient": "agent-b",
        "content": {"name": "Ada Lovelace"},
        "vault": policy.field_values,
        "allowed_fields": policy.allowed_fields,
        "policy_id": policy.policy_id,
        "flow_items": policy_to_flow_items(policy, fields={"name"}),
        "run_id": "run-1",
        "system": "AL-S3",
        "model": "mock-model",
    }
    defaults.update(overrides)
    return AgentLeakTraceEvent(**defaults)


@pytest.mark.asyncio
async def test_allowed_event_is_forwarded_without_content_changes():
    policy = _scenario()
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(policy)

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.ALLOW
    assert result.forwarded is True
    assert result.sanitized_event is not None
    assert result.sanitized_event.content == event.content
    assert result.decision_log["verdict"] == "allow"
    assert result.decision_log["flow_item_field_names"] == ["name"]


@pytest.mark.asyncio
async def test_masked_field_repairs_sanitized_event():
    policy = _scenario(forbidden_action="mask")
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(
        policy,
        content={"name": "Ada Lovelace", "ssn": "123-45-6789"},
        flow_items=policy_to_flow_items(policy, fields={"ssn"}),
    )

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.REPAIR
    assert result.forwarded is True
    assert result.sanitized_event is not None
    assert result.sanitized_event.content["ssn"] == "[REDACTED]"
    assert result.sanitized_event.vault["ssn"] == "[REDACTED]"
    assert result.decision_log["masked_fields"] == ["ssn"]


@pytest.mark.asyncio
async def test_denied_uri_blocks_event_and_drops_item():
    policy = _scenario()
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(
        policy,
        content={"ssn": "123-45-6789"},
        flow_items=policy_to_flow_items(policy, fields={"ssn"}),
    )

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.BLOCK
    assert result.forwarded is False
    assert result.sanitized_event is None
    assert result.decision_log["dropped_uris"] == [policy.uri_by_field["ssn"]]


@pytest.mark.asyncio
async def test_c2_event_uses_recipient_as_acl_subject():
    policy = _scenario()
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(policy, actor="sender-agent", recipient="receiver-agent")

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.ALLOW
    assert runtime.acl.calls == [(policy.uri_by_field["name"], "receiver-agent")]


@pytest.mark.asyncio
async def test_missing_recipient_uses_actor_as_acl_subject():
    policy = _scenario()
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(policy, actor="sender-agent", recipient=None)

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.ALLOW
    assert runtime.acl.calls == [(policy.uri_by_field["name"], "sender-agent")]


@pytest.mark.asyncio
async def test_decision_log_excludes_raw_sensitive_payload_values():
    policy = _scenario(forbidden_action="mask")
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(
        policy,
        content={"ssn": "123-45-6789"},
        flow_items=policy_to_flow_items(policy, fields={"ssn"}),
    )

    result = await runtime.enforce_event(event)
    serialized = json.dumps(result.decision_log, sort_keys=True)

    assert "123-45-6789" not in serialized
    assert "Ada Lovelace" not in serialized
    assert result.decision_log["sanitized_payload_ref"].startswith("sha256:")
    assert result.flow_payload_summary == {
        "item_count": 1,
        "uris": [policy.uri_by_field["ssn"]],
        "field_names": ["ssn"],
    }


@pytest.mark.asyncio
async def test_paraphrased_free_text_without_provenance_is_diagnostic_only():
    policy = _scenario()
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(
        policy,
        content="The patient identifier was paraphrased here.",
        vault={},
        flow_items=[],
    )

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.ALLOW
    assert result.forwarded is True
    assert result.sanitized_event is not None
    assert result.sanitized_event.metadata["semantic_unmapped"] is True
    assert result.decision_log["semantic_unmapped"] is True
    assert result.decision_log["flow_item_uris"] == []


@pytest.mark.asyncio
async def test_c6_log_event_records_protocol_pseudo_boundary():
    policy = _scenario()
    runtime = AgentLeakFlowRuntime(policy)
    event = _event(policy, channel=AgentLeakChannel.C6)

    result = await runtime.enforce_event(event)

    assert result.decision.verdict == Verdict.ALLOW
    assert result.decision_log["boundary"] == "log_persistence"
