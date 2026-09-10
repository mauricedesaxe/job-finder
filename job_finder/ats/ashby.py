from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from job_finder.ats.models import AtsAvailable, normalize_workplace_type, unique_locations
from job_finder.urls import parse_http_url


class _AshbyWireModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True)


class _AshbySecondaryLocation(_AshbyWireModel):
    location: str


class _AshbyPostalAddress(_AshbyWireModel):
    address_country: str | None = Field(default=None, alias="addressCountry")


class _AshbyAddress(_AshbyWireModel):
    postal_address: _AshbyPostalAddress | None = Field(default=None, alias="postalAddress")


class _AshbyJob(_AshbyWireModel):
    id: str
    location: str | None = None
    secondary_locations: list[_AshbySecondaryLocation] | None = Field(
        default=None, alias="secondaryLocations"
    )
    workplace_type: str | None = Field(default=None, alias="workplaceType")
    address: _AshbyAddress | None = None


class _AshbyResponse(_AshbyWireModel):
    jobs: list[_AshbyJob]


def parse_ashby_url(url: str) -> tuple[str, str] | None:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return None
    if parsed.hostname != "jobs.ashbyhq.com":
        return None
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if len(segments) < 2:
        return None
    return segments[0], segments[1]


def parse_ashby_job(payload: object, job_id: str) -> AtsAvailable | None:
    response = _AshbyResponse.model_validate(payload)
    job = next((candidate for candidate in response.jobs if candidate.id == job_id), None)
    if job is None:
        return None
    primary = job.location or ""
    secondary = tuple(location.location for location in job.secondary_locations or ())
    country = (
        job.address.postal_address.address_country
        if job.address and job.address.postal_address
        else None
    )
    return AtsAvailable(
        source="ashby",
        location=primary,
        locations=unique_locations(primary, secondary),
        workplace_type=normalize_workplace_type(job.workplace_type),
        country=country,
    )
