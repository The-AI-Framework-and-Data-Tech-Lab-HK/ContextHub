import pytest

from integrations.agentleak.policy_compiler import compile_policy


def _scenario() -> dict:
    return {
        "scenario_id": "medical-case-7",
        "private_vault": {
            "records": [
                {
                    "fields": {
                        "patient_name": "Alice Example",
                        "ssn": "123-45-6789",
                        "diagnosis": "Z99.1",
                    }
                }
            ]
        },
        "allowed_set": {
            "fields": ["patient_name"],
            "forbidden_fields": ["ssn"],
        },
    }


def test_compile_policy_marks_allowed_forbidden_and_implicit_fields() -> None:
    policy = compile_policy(_scenario())

    assert policy.scenario_id == "medical-case-7"
    assert policy.policy_id == "agentleak-policy:medical-case-7"
    assert policy.uri_by_field == {
        "diagnosis": "ctx://agentleak/medical-case-7/record_000/diagnosis",
        "patient_name": "ctx://agentleak/medical-case-7/record_000/patient_name",
        "ssn": "ctx://agentleak/medical-case-7/record_000/ssn",
    }
    assert policy.allowed_fields == {"patient_name"}
    assert policy.forbidden_fields == {"diagnosis", "ssn"}
    assert policy.field_actions == {
        "diagnosis": "deny",
        "patient_name": "allow",
        "ssn": "deny",
    }
    assert policy.metadata["implicit_forbidden"] is True
    assert policy.metadata["implicit_forbidden_fields"] == ["diagnosis"]
    assert policy.metadata["uses_online_llm_or_detector"] is False


def test_compile_policy_can_mask_forbidden_fields_for_ablation() -> None:
    policy = compile_policy(_scenario(), forbidden_action="mask")

    assert policy.field_actions["ssn"] == "mask"
    assert policy.field_actions["diagnosis"] == "mask"
    assert policy.metadata["forbidden_action"] == "mask"


def test_compile_policy_rejects_allow_as_forbidden_action() -> None:
    with pytest.raises(ValueError, match="forbidden_action"):
        compile_policy(_scenario(), forbidden_action="allow")


def test_compile_policy_disambiguates_duplicate_field_names() -> None:
    scenario = {
        "scenario_id": "duplicate-fields",
        "private_vault": {
            "records": [
                {"fields": {"account_id": "A-1"}},
                {"fields": {"account_id": "A-2"}},
            ]
        },
        "allowed_set": {
            "fields": ["record_000.account_id"],
            "forbidden_fields": ["record_001.account_id"],
        },
    }

    policy = compile_policy(scenario)

    assert policy.uri_by_field == {
        "record_000.account_id": (
            "ctx://agentleak/duplicate-fields/record_000/account_id"
        ),
        "record_001.account_id": (
            "ctx://agentleak/duplicate-fields/record_001/account_id"
        ),
    }
    assert policy.allowed_fields == {"record_000.account_id"}
    assert policy.forbidden_fields == {"record_001.account_id"}
