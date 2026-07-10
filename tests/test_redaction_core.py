"""Tests for the shared value-redaction core (contexthub.redaction)."""
from contexthub.redaction import (
    BLOCKED_TOKEN,
    MASK_TOKEN,
    block_if_present,
    redact_value_tree,
    redact_values,
)
from contexthub.redaction.generic import GenericRedactor, redact_generic


def test_redact_values_case_insensitive() -> None:
    assert redact_values("Ada and ADA and ada", ["ada"]) == f"{MASK_TOKEN} and {MASK_TOKEN} and {MASK_TOKEN}"


def test_redact_values_longest_first_avoids_partial_clobber() -> None:
    # "Ada Lovelace" must be masked whole, not leave "Lovelace" behind after
    # masking the shorter substring "Ada".
    out = redact_values("Ada Lovelace here", ["Ada", "Ada Lovelace"])
    assert out == f"{MASK_TOKEN} here"


def test_redact_values_empty_and_none_values_ignored() -> None:
    assert redact_values("keep this", ["", None]) == "keep this"
    assert redact_values("", ["x"]) == ""


def test_block_if_present() -> None:
    assert block_if_present("has 123-45-6789 in it", ["123-45-6789"]) == BLOCKED_TOKEN
    assert block_if_present("nothing here", ["secret"]) == "nothing here"
    assert block_if_present("CASE-INSENSITIVE match", ["case-insensitive"]) == BLOCKED_TOKEN
    assert block_if_present("", ["x"]) == ""


def test_redact_value_tree_recurses_str_dict_list() -> None:
    payload = {
        "note": "secret value here",
        "items": ["secret", "safe", {"deep": "secret nested"}],
        "n": 42,
    }
    out = redact_value_tree(payload, ["secret"])
    assert out["note"] == f"{MASK_TOKEN} value here"
    assert out["items"][0] == MASK_TOKEN
    assert out["items"][1] == "safe"
    assert out["items"][2]["deep"] == f"{MASK_TOKEN} nested"
    assert out["n"] == 42


def test_generic_redactor_masks_pii_and_canary() -> None:
    result = GenericRedactor().redact(
        "ssn 123-45-6789 mail a@b.com CANARY_ABC phone 415-555-1234"
    )
    assert "[REDACTED:SSN]" in result.content
    assert "[REDACTED:EMAIL]" in result.content
    assert "[REDACTED:CANARY]" in result.content
    assert "[REDACTED:PHONE]" in result.content
    assert result.redacted is True


def test_generic_luhn_invalid_card_left_intact() -> None:
    # Luhn-invalid number must NOT be redacted (over-redaction guard).
    result = redact_generic("Reference 4111 1111 1111 1112 is not a valid card.")
    assert "4111 1111 1111 1112" in result.content
    assert not any(m.pattern == "credit_card" for m in result.matches)


def test_generic_summary_flags_policy_blind() -> None:
    summary = redact_generic("ssn 123-45-6789").summary()
    assert summary["uses_allowed_set"] is False
    assert summary["uses_online_llm_or_detector"] is False
