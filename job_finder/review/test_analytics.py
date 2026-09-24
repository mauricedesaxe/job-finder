from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from job_finder.review.analytics import DayModelSpend, DaySpend, ModelSpend, SpendAnalytics

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_day_spend_rejects_outcomes_that_do_not_add_up_to_the_calls() -> None:
    with pytest.raises(ValueError, match="outcomes do not add up"):
        DaySpend(
            day=NOW.date(),
            calls=3,
            accepted=1,
            errors=1,
            known_cost_usd=Decimal("0.50"),
            by_model=(),
        )


def test_day_spend_rejects_negative_spend() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        DaySpend(
            day=NOW.date(),
            calls=2,
            accepted=2,
            errors=0,
            known_cost_usd=Decimal("-0.01"),
            by_model=(),
        )


def test_day_spend_rejects_a_model_breakdown_that_does_not_add_up() -> None:
    with pytest.raises(ValueError, match="model spend does not add up"):
        DaySpend(
            day=NOW.date(),
            calls=2,
            accepted=2,
            errors=0,
            known_cost_usd=Decimal("0.50"),
            by_model=(DayModelSpend(model="glm-4.6", known_cost_usd=Decimal("0.25")),),
        )


def test_day_model_spend_rejects_negative_spend() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        DayModelSpend(model="glm-4.6", known_cost_usd=Decimal("-0.01"))


def test_day_model_spend_rejects_a_blank_model_name() -> None:
    with pytest.raises(ValueError, match="model name must not be empty"):
        DayModelSpend(model="", known_cost_usd=Decimal("0.25"))


def test_model_spend_rejects_a_blank_model_name() -> None:
    with pytest.raises(ValueError, match="model name must not be empty"):
        ModelSpend(
            model="",
            calls=1,
            accepted=1,
            errors=0,
            input_tokens=10,
            output_tokens=5,
            known_cost_usd=Decimal("0.25"),
            max_latency_ms=40,
        )


def test_spend_analytics_rejects_totals_that_do_not_add_up() -> None:
    with pytest.raises(ValueError, match="outcomes do not add up"):
        SpendAnalytics(
            known_usd=Decimal("1.00"),
            calls=5,
            accepted=3,
            errors=1,
            input_tokens=100,
            output_tokens=50,
            max_latency_ms=900,
            days=(),
            models=(),
        )


def test_spend_analytics_rejects_negative_totals() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        SpendAnalytics(
            known_usd=Decimal("-1.00"),
            calls=0,
            accepted=0,
            errors=0,
            input_tokens=0,
            output_tokens=0,
            max_latency_ms=0,
            days=(),
            models=(),
        )


def test_spend_analytics_accepts_a_consistent_zero_state() -> None:
    spend = SpendAnalytics(
        known_usd=Decimal(0),
        calls=0,
        accepted=0,
        errors=0,
        input_tokens=0,
        output_tokens=0,
        max_latency_ms=0,
        days=(),
        models=(),
    )

    assert spend.calls == 0
