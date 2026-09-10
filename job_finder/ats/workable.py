from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from job_finder.ats.models import AtsAvailable, normalize_workplace_type, unique_locations
from job_finder.urls import parse_http_url


class _WorkableWireModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True)


class _WorkableLocation(_WorkableWireModel):
    country: str | None = None
    city: str | None = None


class _WorkableJob(_WorkableWireModel):
    shortcode: str
    workplace: str | None = None
    location: _WorkableLocation | None = None
    locations: list[_WorkableLocation] | None = None


class _WorkableResponse(_WorkableWireModel):
    results: list[_WorkableJob]


def parse_workable_url(url: str) -> tuple[str, str] | None:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return None
    if parsed.hostname != "apply.workable.com":
        return None
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    try:
        job_index = segments.index("j")
    except ValueError:
        return None
    if job_index == 0 or job_index == len(segments) - 1:
        return None
    return segments[job_index - 1], segments[job_index + 1]


def parse_workable_job(payload: object, shortcode: str) -> AtsAvailable | None:
    response = _WorkableResponse.model_validate(payload)
    job = next(
        (candidate for candidate in response.results if candidate.shortcode == shortcode), None
    )
    if job is None:
        return None
    primary = _format_location(job.location)
    secondary = tuple(
        location for item in job.locations or () if (location := _format_location(item))
    )
    return AtsAvailable(
        source="workable",
        location=primary,
        locations=unique_locations(primary, secondary),
        workplace_type=normalize_workplace_type(
            job.workplace,
            onsite_values=("onsite", "on-site", "on_site"),
        ),
        country=job.location.country if job.location else None,
    )


def _format_location(location: _WorkableLocation | None) -> str:
    if location is None:
        return ""
    return ", ".join(part for part in (location.city, location.country) if part)
