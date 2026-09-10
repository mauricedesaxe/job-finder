from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

AtsSource = Literal["lever", "ashby", "greenhouse", "workable"]
WorkplaceType = Literal["Remote", "Hybrid", "OnSite"]


class AtsModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class AtsAvailable(AtsModel):
    kind: Literal["available"] = "available"
    source: AtsSource
    location: str
    locations: tuple[str, ...]
    workplace_type: WorkplaceType | None
    country: str | None


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
