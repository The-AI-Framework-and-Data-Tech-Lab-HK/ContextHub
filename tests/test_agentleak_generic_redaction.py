from integrations.agentleak.generic_redaction import redact_generic


def test_generic_redaction_masks_canary_ssn_email_phone_and_card() -> None:
    result = redact_generic(
        {
            "message": (
                "AGENTLEAK_CANARY_abc ssn 123-45-6789 email alice@example.com "
                "phone (415) 555-0123 card 4111 1111 1111 1111"
            )
        }
    )

    assert result.content == {
        "message": (
            "[REDACTED:CANARY] ssn [REDACTED:SSN] email [REDACTED:EMAIL] "
            "phone [REDACTED:PHONE] card [REDACTED:CREDIT_CARD]"
        )
    }
    assert result.summary()["patterns"] == ["canary", "credit_card", "email", "phone", "ssn"]
    assert result.summary()["uses_allowed_set"] is False
    assert result.summary()["uses_online_llm_or_detector"] is False


def test_generic_redaction_does_not_obviously_redact_business_ids() -> None:
    result = redact_generic("Order ORDER-2026-0001 maps to project CTX-42 and invoice INV-100200.")

    assert result.content == "Order ORDER-2026-0001 maps to project CTX-42 and invoice INV-100200."
    assert result.redacted is False


def test_credit_card_candidates_require_luhn_and_record_overredaction_candidate() -> None:
    result = redact_generic("Reference number 4111 1111 1111 1112 is not a valid card.")

    assert result.content == "Reference number 4111 1111 1111 1112 is not a valid card."
    assert result.redacted is False
    assert result.over_redaction_candidates == [
        {"pattern": "credit_card", "path": "$", "reason": "luhn_failed"}
    ]
