from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import ClassVar, Literal

import requests
from pydantic import BaseModel, ConfigDict, ValidationError

FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD"
DEFAULT_RATES: dict[str, Decimal] = {
    "EUR": Decimal("1.10"),
    "GBP": Decimal("1.27"),
}
PROMPT_CURRENCIES = (
    "EUR",
    "GBP",
    "CHF",
    "CAD",
    "AUD",
    "PLN",
    "SEK",
    "NOK",
    "DKK",
    "CZK",
    "SGD",
    "ILS",
)


class RateModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class FrankfurterRates(RateModel):
    rates: dict[str, Decimal]


class ExchangeRateSnapshot(RateModel):
    rates: dict[str, Decimal]
    source: Literal["frankfurter", "fallback"]
    observed_at: datetime


class RateHttpResponse(RateModel):
    status_code: int
    body: str


RateSender = Callable[[str, float], RateHttpResponse]


def fetch_exchange_rates(
    *,
    observed_at: datetime,
    sender: RateSender | None = None,
) -> ExchangeRateSnapshot:
    send = sender or send_rate_request
    try:
        response = send(FRANKFURTER_URL, 30.0)
        if response.status_code != 200:
            raise ValueError(f"Frankfurter returned HTTP {response.status_code}")
        payload = FrankfurterRates.model_validate_json(response.body)
        rates = {
            currency: (Decimal(1) / per_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for currency, per_usd in payload.rates.items()
            if per_usd > 0
        }
        if not rates:
            raise ValueError("Frankfurter returned no positive rates")
        return ExchangeRateSnapshot(
            rates=rates,
            source="frankfurter",
            observed_at=observed_at,
        )
    except (requests.RequestException, ValidationError, ValueError, ZeroDivisionError):
        return ExchangeRateSnapshot(
            rates=DEFAULT_RATES,
            source="fallback",
            observed_at=observed_at,
        )


def format_compensation_rates(rates: Mapping[str, Decimal]) -> str:
    return ", ".join(
        f"1 {currency} ≈ {rates[currency]:.2f} USD"
        for currency in PROMPT_CURRENCIES
        if currency in rates
    )


def send_rate_request(url: str, timeout_seconds: float) -> RateHttpResponse:
    response = requests.get(url, timeout=timeout_seconds)
    return RateHttpResponse(status_code=response.status_code, body=response.text)
