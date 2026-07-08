from integrations.agentleak.metrics import compute_metrics


def test_agentleak_metrics_from_mock_normalized_traces():
    events = [
        {
            "trace_id": "t1",
            "scenario_id": "s1",
            "channel": "C1",
            "leaked": False,
            "metadata": {"task_success": True, "latency_overhead_ms": 2.0},
        },
        {
            "trace_id": "t1",
            "scenario_id": "s1",
            "channel": "C2",
            "leaked": True,
            "leakage_labels": {
                "structured_mediated": True,
                "structured_provenance": True,
            },
            "metadata": {"task_success": True},
        },
        {
            "trace_id": "t2",
            "scenario_id": "s2",
            "channel": "C1",
            "leaked": True,
            "leakage_labels": {"semantic_free_text_residual": True},
            "metadata": {"task_success": False},
        },
        {
            "trace_id": "t2",
            "scenario_id": "s2",
            "channel": "C2",
            "leaked": False,
            "metadata": {"task_success": False},
        },
    ]
    decisions = [
        {"verdict": "allow", "false_block": False, "over_redaction": False},
        {"verdict": "block", "false_block": True, "over_redaction": False},
        {"verdict": "repair", "false_block": False, "over_redaction": True},
    ]

    metrics = compute_metrics(events, decisions, channels=("C1", "C2", "C5"))

    assert metrics["n_traces"] == 2
    assert metrics["channel_leakage_rate"]["C1"]["rate"] == 0.5
    assert metrics["channel_leakage_rate"]["C2"]["rate"] == 0.5
    assert metrics["channel_leakage_rate"]["C5"]["skipped_reason"] == "channel_not_observed"
    assert metrics["exact_leakage_rate"] == 1.0
    assert metrics["internal_leakage_rate"] == 0.5
    assert metrics["audit_gap"] == 0.5
    assert metrics["final_output_safe_but_internal_leaked_rate"] == 0.5
    assert metrics["structured_mediated_leakage_rate"]["rate"] == 0.5
    assert metrics["semantic_free_text_residual_rate"]["rate"] == 0.5
    assert metrics["utility_under_masking"]["value"] == 0.5
    assert metrics["false_block_rate"]["rate"] == 1 / 3
    assert metrics["over_redaction_rate"]["rate"] == 1 / 3
    assert metrics["decision_distribution"] == {"allow": 1, "block": 1, "repair": 1}


def test_missing_decision_and_token_fields_are_explicitly_skipped():
    metrics = compute_metrics(
        [{"trace_id": "t1", "channel": "C2", "leaked": False}],
        [],
        channels=("C2",),
    )

    assert metrics["false_block_rate"]["skipped_reason"] == "decision_log_missing"
    assert metrics["over_redaction_rate"]["skipped_reason"] == "decision_log_missing"
    assert metrics["token_overhead"]["value"] is None
    assert metrics["token_overhead"]["skipped_reason"] == "token_fields_missing"


def test_protocol_nested_agentleak_eval_and_internal_channels():
    events = [
        {
            "trace_id": "t1",
            "scenario_id": "s1",
            "channel": "C1",
            "agentleak_eval": {
                "has_leak": False,
                "leaked_fields": [],
                "detector_mode": "exact",
            },
        },
        {
            "trace_id": "t1",
            "scenario_id": "s1",
            "channel": "C3",
            "agentleak_eval": {
                "has_leak": True,
                "leaked_fields": ["record_000.ssn"],
                "detector_mode": "hybrid",
            },
        },
        {
            "trace_id": "t2",
            "scenario_id": "s2",
            "channel": "C1",
            "agentleak_eval": {
                "has_leak": False,
                "leaked_fields": [],
                "detector_mode": "exact",
            },
        },
        {
            "trace_id": "t2",
            "scenario_id": "s2",
            "channel": "C6",
            "agentleak_eval": {
                "has_leak": True,
                "leaked_fields": [],
                "detector_mode": "llm_only",
            },
            "metadata": {"leakage_type": "semantic"},
        },
    ]

    metrics = compute_metrics(events, [], channels=("C1", "C2", "C3", "C5", "C6"))

    assert metrics["internal_channels"] == ["C2", "C3", "C5", "C6"]
    assert metrics["channel_leakage_rate"]["C3"]["rate"] == 1.0
    assert metrics["channel_leakage_rate"]["C6"]["rate"] == 1.0
    assert metrics["internal_leakage_rate"] == 1.0
    assert metrics["audit_gap"] == 1.0
    assert metrics["final_output_safe_but_internal_leaked_rate"] == 1.0
    assert metrics["structured_mediated_leakage_rate"]["rate"] == 0.5
    assert metrics["semantic_free_text_residual_rate"]["rate"] == 0.5
    assert metrics["detector_mode_distribution"] == {"exact": 2, "hybrid": 1, "llm_only": 1}

