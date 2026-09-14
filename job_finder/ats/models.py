from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

AtsSource = Literal["lever", "ashby", "greenhouse", "workable"]
WorkplaceType = Literal["Remote", "Hybrid", "OnSite"]
CompensationPeriod = Literal["year", "month", "week", "day", "hour"]


class AtsModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class CompensationObservation(AtsModel):
    minimum: int | None = None
    maximum: int | None = None
    currency: str | None = None
    period: CompensationPeriod | None = None


class AtsAvailable(AtsModel):
    kind: Literal["available"] = "available"
    source: AtsSource
    location: str
    locations: tuple[str, ...]
    workplace_type: WorkplaceType | None
    country: str | None
    description: str | None = None
    compensation: CompensationObservation | None = None


_PERIOD_BY_UNIT: dict[str, CompensationPeriod] = {
    "year": "year",
    "month": "month",
    "week": "week",
    "day": "day",
    "hour": "hour",
}


def compensation_period_from_interval(interval: str | None) -> CompensationPeriod | None:
    if interval is None:
        return None
    parts = interval.strip().split()
    if len(parts) != 2:
        return None
    return _PERIOD_BY_UNIT.get(parts[1].lower())


class AtsUnavailable(AtsModel):
    kind: Literal["unavailable"] = "unavailable"
    source: AtsSource
    error_code: str


class AtsNotApplicable(AtsModel):
    kind: Literal["not_applicable"] = "not_applicable"


AtsEvidence = Annotated[
    AtsAvailable | AtsUnavailable | AtsNotApplicable, Field(discriminator="kind")
]


def normalize_workplace_type(
    value: str | None,
    *,
    onsite_values: tuple[str, ...] = ("onsite", "on-site"),
) -> WorkplaceType | None:
    if value is None:
        return None
    normalized = value.lower()
    if normalized == "remote":
        return "Remote"
    if normalized == "hybrid":
        return "Hybrid"
    if normalized in onsite_values:
        return "OnSite"
    return None


def unique_locations(primary: str, secondary: tuple[str, ...]) -> tuple[str, ...]:
    if primary and primary not in secondary:
        return (primary, *secondary)
    return secondary
