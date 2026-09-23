from decimal import Decimal

import pytest
from pydantic import ValidationError

from contexthub.models.execution import Cost, PriceBasis, Usage, calculate_cost
from knowledge_helpers import NOW


def price(**kwargs):
    return PriceBasis(
        source="fixture",
        observed_at=NOW,
        currency="TEST",
        region="test",
        billing_tier="synthetic",
        model="model-snapshot",
        unit_tokens=1000,
        input_rate=Decimal("2"),
        output_rate=Decimal("3"),
        **kwargs,
    )


def test_unknown_usage_or_price_is_not_zero():
    missing = Usage(completeness="missing", missing_reason="provider_not_returned")
    known = Usage(input_tokens=10, output_tokens=5, completeness="complete")
    assert calculate_cost(missing, price(), "model-snapshot").amount is None
    assert calculate_cost(known, None, "model-snapshot").amount is None
    assert calculate_cost(known, price(), None).reason == "price_model_mismatch"
    assert calculate_cost(known, price(), "model-snapshot").amount == Decimal("0.035")
    with pytest.raises(ValidationError):
        Cost(status="unknown", amount=0, reason="missing")


def test_partial_and_cached_usage():
    partial = Usage(
        input_tokens=2, completeness="partial", missing_reason="output_missing"
    )
    assert calculate_cost(partial, price(), "model-snapshot").amount is None
    cached = Usage(
        input_tokens=10, output_tokens=5, cached_input_tokens=5, completeness="complete"
    )
    assert (
        calculate_cost(cached, price(), "model-snapshot").reason
        == "cached_price_missing"
    )
    assert calculate_cost(
        cached, price(cached_input_rate=Decimal("1")), "model-snapshot"
    ).amount == Decimal("0.030")
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1, output_tokens=0, completeness="complete")
    with pytest.raises(ValidationError):
        Usage(input_tokens=True, output_tokens=0, completeness="complete")
