from integrations.agentleak.mapping import channel_to_boundary, event_to_flow_payload
from integrations.agentleak.policy_compiler import compile_policy
from integrations.agentleak.trace_schema import AgentLeakChannel, AgentLeakTraceEvent
from contexthub.enforcement.context import Boundary


def _scenario() -> dict:
    return {
        "scenario_id": "fixture-001",
        "private_vault": {
            "records": [
                {
                    "fields": {
                        "patient_name": "Alice Example",
                        "diagnosis": "Z99.1",
                    }
                }
            ]
        },
        "allowed_set": {"fields": ["patient_name"], "forbidden_fields": ["diagnosis"]},
    }


def test_trace_event_serializes_normalized_channel_and_sets() -> None:
    event = AgentLeakTraceEvent(
        trace_id="trace-1",
        scenario_id="fixture-001",
        channel="C2",
        actor="agent-a",
        recipient="agent-b",
        content="handoff includes only the allowed name",
        vault={"patient_name": "Alice Example"},
        allowed_fields={"patient_name"},
        metadata={"raw": {"unexpected": "kept"}},
    )

    assert event.channel == AgentLeakChannel.C2
    assert event.to_json()["channel"] == "C2"
    assert event.to_json()["run_id"] == "fixture-run"
    assert event.to_json()["event_type"] == "agent_message"
    assert event.to_json()["source"] == "agent-a"
    assert event.to_json()["target"] == "agent-b"
    assert event.to_json()["content_ref"] == "unmaterialized://trace-1/C2"
    assert event.to_json()["agentleak_eval"] == {
        "has_leak": None,
        "leaked_fields": [],
        "detector_mode": "not_run",
    }
    assert event.to_protocol_json()["content_ref"] == "unmaterialized://trace-1/C2"
    assert event.to_json()["allowed_fields"] == ["patient_name"]
    assert event.metadata["raw"] == {"unexpected": "kept"}


def test_channel_to_boundary_mapping_covers_c1_c2_c5() -> None:
    assert channel_to_boundary(AgentLeakChannel.C1) is None
    assert channel_to_boundary(AgentLeakChannel.C2) == Boundary.HANDOFF
    assert channel_to_boundary(AgentLeakChannel.C5) == Boundary.SHARED_MEMORY_WRITE


def test_event_to_flow_payload_projects_structured_vault_only() -> None:
    policy = compile_policy(_scenario())
    event = AgentLeakTraceEvent(
        trace_id="trace-1",
        scenario_id="fixture-001",
        channel=AgentLeakChannel.C2,
        actor="agent-a",
        recipient="agent-b",
        content="free text is not semantically parsed by Task 2",
        vault=policy.field_values,
        allowed_fields=policy.allowed_fields,
        policy_id=policy.policy_id,
    )

    payload = event_to_flow_payload(event, policy)

    assert payload == {
        "items": [
            {
                "uri": "ctx://agentleak/fixture-001/record_000/diagnosis",
                "fields": {"diagnosis": "Z99.1"},
            },
            {
                "uri": "ctx://agentleak/fixture-001/record_000/patient_name",
                "fields": {"patient_name": "Alice Example"},
            },
        ]
    }
