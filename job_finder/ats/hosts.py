from job_finder.ats.models import AtsSource

_ATS_SOURCE_BY_HOSTNAME: dict[str, AtsSource] = {
    "jobs.ashbyhq.com": "ashby",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "apply.workable.com": "workable",
}


def ats_source_for_hostname(hostname: str | None) -> AtsSource | None:
    if hostname is None:
        return None
    return _ATS_SOURCE_BY_HOSTNAME.get(hostname)
