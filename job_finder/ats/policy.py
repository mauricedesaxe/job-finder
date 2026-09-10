from __future__ import annotations

from job_finder.ats.models import AtsAvailable, AtsEvidence, AtsSource
from job_finder.jobs.models import StructuralDecision, StructuralPass, StructuralRejection
from job_finder.urls import parse_http_url


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
    if isinstance(evidence, AtsAvailable) and evidence.workplace_type == "OnSite":
        location = evidence.location or evidence.country or "unspecified"
        return StructuralRejection(reason=f"ATS workplaceType=OnSite ({location})")
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
    lines.append("---")
    return "\n".join(lines)
