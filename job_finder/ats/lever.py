from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from job_finder.ats.models import AtsAvailable, normalize_workplace_type, unique_locations
from job_finder.urls import parse_http_url


class _LeverWireModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True)


class _LeverCategories(_LeverWireModel):
    location: str | None = None
    all_locations: list[str] | None = Field(default=None, alias="allLocations")


class _LeverJob(_LeverWireModel):
    categories: _LeverCategories | None = None
    workplace_type: str | None = Field(default=None, alias="workplaceType")
    country: str | None = None


def parse_lever_url(url: str) -> tuple[str, str] | None:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return None
    if parsed.hostname != "jobs.lever.co":
        return None
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if len(segments) < 2:
        return None
    return segments[0], segments[1]


def parse_lever_job(payload: object) -> AtsAvailable:
    job = _LeverJob.model_validate(payload)
    primary = job.categories.location if job.categories and job.categories.location else ""
    secondary = tuple(job.categories.all_locations or ()) if job.categories else ()
    return AtsAvailable(
        source="lever",
        location=primary,
        locations=unique_locations(primary, secondary),
        workplace_type=normalize_workplace_type(job.workplace_type),
        country=job.country,
    )
