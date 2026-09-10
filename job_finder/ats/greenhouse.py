from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, StrictInt

from job_finder.ats.models import AtsAvailable, unique_locations
from job_finder.urls import parse_http_url


class _GreenhouseWireModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True)


class _GreenhouseLocation(_GreenhouseWireModel):
    name: str


class _GreenhouseOffice(_GreenhouseWireModel):
    id: StrictInt
    location: str | None = None


class _GreenhouseJob(_GreenhouseWireModel):
    id: StrictInt
    location: _GreenhouseLocation | None = None
    offices: list[_GreenhouseOffice] | None = None


def parse_greenhouse_url(url: str) -> tuple[str, str] | None:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return None
    if parsed.hostname not in (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
    ):
        return None
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    try:
        jobs_index = segments.index("jobs")
    except ValueError:
        return None
    if jobs_index == 0 or jobs_index == len(segments) - 1:
        return None
    return segments[jobs_index - 1], segments[jobs_index + 1]


def parse_greenhouse_job(payload: object) -> AtsAvailable:
    job = _GreenhouseJob.model_validate(payload)
    primary = job.location.name if job.location else ""
    office_locations = tuple(office.location for office in job.offices or () if office.location)
    country = _country_from_location(job.offices[0].location) if job.offices else None
    return AtsAvailable(
        source="greenhouse",
        location=primary,
        locations=unique_locations(primary, office_locations),
        workplace_type=None,
        country=country,
    )


def _country_from_location(location: str | None) -> str | None:
    if not location:
        return None
    parts = tuple(part.strip() for part in location.split(",") if part.strip())
    return parts[-1] if len(parts) > 1 else None
