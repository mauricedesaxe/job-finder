from __future__ import annotations

from datetime import date
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_finder.urls import parse_http_url

JobSource = Literal["ashbyhq", "lever", "greenhouse", "workable", "other"]


class JobModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class JobListing(JobModel):
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


class StructuralPass(JobModel):
    kind: Literal["pass"] = "pass"


class StructuralRejection(JobModel):
    kind: Literal["rejected"] = "rejected"
    reason: str


StructuralDecision = Annotated[StructuralPass | StructuralRejection, Field(discriminator="kind")]
