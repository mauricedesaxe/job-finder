from __future__ import annotations

import re

from job_finder.ats.models import AtsAvailable, AtsEvidence, AtsSource
from job_finder.jobs.structural_filter import (
    StructuralDecision,
    StructuralPass,
    StructuralRejection,
)
from job_finder.urls import parse_http_url

_LOCATION_QUESTION = re.compile(
    r"""remote|hybrid|on.?site|office|residen|authori[sz]|work\s+(?:in|from)|
    visa|sponsor|states?|countr|based|locat|relocat|time.?zone""",
    re.I | re.X,
)


def detect_ats_source(url: str) -> AtsSource | None:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return None
    if parsed.hostname == "jobs.lever.co":
        return "lever"
    if parsed.hostname == "jobs.ashbyhq.com":
        return "ashby"
    if parsed.hostname in (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
    ):
        return "greenhouse"
    if parsed.hostname == "apply.workable.com":
        return "workable"
    return None


def ats_structural_filter(evidence: AtsEvidence) -> StructuralDecision:
    if isinstance(evidence, AtsAvailable) and evidence.workplace_type in ("OnSite", "Hybrid"):
        location = evidence.location or evidence.country or "unspecified"
        return StructuralRejection(
            reason=f"ATS workplaceType={evidence.workplace_type} ({location})"
        )
    return StructuralPass()


def format_ats_block(data: AtsAvailable) -> str:
    lines = [f"## ATS Structured Data (from {data.source} API)"]
    if data.location:
        lines.append(f"- Primary location: {data.location}")
    if data.locations:
        lines.append(f"- All listed locations: {', '.join(data.locations)}")
    lines.append(f"- Workplace type: {data.workplace_type or 'unspecified'}")
    if data.country:
        lines.append(f"- Country fallback when locations are non-geographic: {data.country}")
    for question in data.application_questions:
        if not _LOCATION_QUESTION.search(question.label):
            continue
        requirement = "required" if question.required else "optional"
        choices = f" Choices: {', '.join(question.choices)}" if question.choices else ""
        lines.append(f"- Application question ({requirement}): {question.label}{choices}")
    lines.append("---")
    return "\n".join(lines)


def format_location_context(data: AtsAvailable, listing_description: str) -> str:
    block = format_ats_block(data)
    if data.description and data.description not in listing_description:
        return f"{block}\n\nATS job description:\n{data.description}"
    return block


def format_ats_description(data: AtsAvailable, body: str) -> str:
    return f"{format_ats_block(data)}\n\n{body}"
