from __future__ import annotations

import re
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_finder.jobs.listings import JobListing
from job_finder.urls import parse_http_url


class StructuralPass(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["pass"] = "pass"


class StructuralRejection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["rejected"] = "rejected"
    reason: str


StructuralDecision = Annotated[StructuralPass | StructuralRejection, Field(discriminator="kind")]

_GENERIC_TITLE_PATTERNS = (
    re.compile(r"\bgeneral application\b", re.I),
    re.compile(r"\bopen application\b", re.I),
    re.compile(r"\btalent (pool|community|network)\b", re.I),
    re.compile(r"\bfuture opportunities\b", re.I),
    re.compile(r"\bjoin our talent\b", re.I),
    re.compile(r"\bjoin (?:the|our) team\b", re.I),
)
_ROLE_TITLE = re.compile(
    r"""\b(?:engineer|developer|architect|lead|senior|staff|backend|frontend|full.?stack|
    web3|devops|sre|manager)\b""",
    re.I | re.X,
)
_OUT_OF_SCOPE_TITLE_PATTERNS = (
    re.compile(r"\bmachine learning engineer\b", re.I),
    re.compile(r"\bdata engineer\b", re.I),
    re.compile(r"\bdevops\b", re.I),
    re.compile(r"\bsite reliability\b", re.I),
    re.compile(r"\bsoftware architect\b", re.I),
    re.compile(r"\bengineering team lead\b", re.I),
    re.compile(r"\bsoftware engineer team lead\b", re.I),
    re.compile(r"\bai automation engineer\b", re.I),
    re.compile(r"\bsenior llm engineer\b", re.I),
    re.compile(r"\bml systems engineer\b", re.I),
    re.compile(r"\bagent platform\b", re.I),
    re.compile(r"\bdecentrali[sz]ed messaging engineer\b", re.I),
)
GENERIC_TITLE_REASON = "Generic / talent-pool title"
NON_ROLE_TITLE_REASON = "Title does not identify a role"


def structural_filter(job: JobListing) -> StructuralDecision:
    parsed = parse_http_url(job.url)
    if parsed is None or parsed.hostname is None:
        return StructuralRejection(reason=f"Invalid job URL ({job.url})")
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if parsed.hostname == "jobs.lever.co" and segments[:1] in (("jobgether",), ("toptal",)):
        return StructuralRejection(reason=f"Aggregator/marketplace listing ({job.url})")
    if _is_careers_index(parsed.hostname, segments):
        return StructuralRejection(reason=f"Careers-index page, not a specific role ({job.url})")
    if any(pattern.search(job.title) for pattern in _GENERIC_TITLE_PATTERNS):
        return StructuralRejection(reason=f"{GENERIC_TITLE_REASON} ({job.title})")
    if _ROLE_TITLE.search(job.title) is None:
        return StructuralRejection(reason=f"{NON_ROLE_TITLE_REASON} ({job.title})")
    if any(pattern.search(job.title) for pattern in _OUT_OF_SCOPE_TITLE_PATTERNS):
        return StructuralRejection(reason=f"Out-of-scope role title ({job.title})")
    return StructuralPass()


def _is_careers_index(hostname: str, segments: tuple[str, ...]) -> bool:
    if len(segments) != 1:
        return False
    return (
        hostname == "apply.workable.com"
        or hostname == "jobs.lever.co"
        or hostname
        in (
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "boards.eu.greenhouse.io",
        )
        or hostname == "jobs.ashbyhq.com"
    )
