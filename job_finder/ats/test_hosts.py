import pytest

from job_finder.ats.hosts import ats_source_for_hostname
from job_finder.ats.models import AtsSource


@pytest.mark.parametrize(
    ("hostname", "source"),
    (
        ("jobs.ashbyhq.com", "ashby"),
        ("boards.greenhouse.io", "greenhouse"),
        ("job-boards.greenhouse.io", "greenhouse"),
        ("boards.eu.greenhouse.io", "greenhouse"),
        ("jobs.lever.co", "lever"),
        ("apply.workable.com", "workable"),
    ),
)
def test_identifies_exact_ats_hostnames(hostname: str, source: AtsSource) -> None:
    assert ats_source_for_hostname(hostname) == source


@pytest.mark.parametrize(
    "hostname",
    (None, "greenhouse.io", "evil.jobs.lever.co", "jobs.ashbyhq.com.example.com"),
)
def test_rejects_unknown_ats_hostnames(hostname: str | None) -> None:
    assert ats_source_for_hostname(hostname) is None
