from __future__ import annotations

from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from job_finder.urls import parse_http_url

JobSource = Literal["ashbyhq", "lever", "greenhouse", "workable", "other"]


class JobListing(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    title: str
    company: str
    url: str
    source: JobSource
    keywords_matched: tuple[str, ...]
    date_posted: date | None
    date_scraped: date
    description: str
    location: str = ""
    profile: str = ""

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        if parse_http_url(value) is None:
            raise ValueError("Job URL must use HTTP or HTTPS")
        return value
