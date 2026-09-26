from datetime import UTC, datetime
from decimal import Decimal
import logging

import requests
from pytest import LogCaptureFixture

from job_finder.discovery.exchange_rates import (
    RateHttpResponse,
    fetch_exchange_rates,
    format_compensation_rates,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_inverts_and_freezes_frankfurter_rates() -> None:
    snapshot = fetch_exchange_rates(
        observed_at=NOW,
        sender=lambda _url, _timeout: RateHttpResponse(
            status_code=200,
            body='{"rates":{"EUR":0.8,"GBP":0.5,"ZERO":0}}',
        ),
    )

    assert snapshot.source == "frankfurter"
    assert snapshot.rates == {"EUR": Decimal("1.25"), "GBP": Decimal("2.00")}
    assert snapshot.observed_at == NOW
    assert format_compensation_rates(snapshot.rates) == "1 EUR ≈ 1.25 USD, 1 GBP ≈ 2.00 USD"


def test_uses_frozen_fallback_rates_for_invalid_responses(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    snapshot = fetch_exchange_rates(
        observed_at=NOW,
        sender=lambda _url, _timeout: RateHttpResponse(status_code=503, body="unavailable"),
    )

    assert snapshot.source == "fallback"
    assert snapshot.rates == {"EUR": Decimal("1.10"), "GBP": Decimal("1.27")}
    assert "Exchange rate fetch failed (ValueError); using fallback rates" in caplog.text


def test_fallback_log_does_not_expose_network_error_details(caplog: LogCaptureFixture) -> None:
    def unavailable(_url: str, _timeout: float) -> RateHttpResponse:
        raise requests.RequestException("password=secret-value")

    snapshot = fetch_exchange_rates(observed_at=NOW, sender=unavailable)

    assert snapshot.source == "fallback"
    assert "RequestException" in caplog.text
    assert "secret-value" not in caplog.text
