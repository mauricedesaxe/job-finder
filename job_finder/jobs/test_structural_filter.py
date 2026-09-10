from datetime import date

import pytest

from job_finder.jobs.models import JobListing
from job_finder.jobs.structural_filter import structural_filter


def _job(
    *, title: str = "Senior Backend Engineer", url: str = "https://example.com/job"
) -> JobListing:
    return JobListing(
        title=title,
        company="Acme",
        url=url,
        source="other",
        keywords_matched=("test",),
        date_posted=None,
        date_scraped=date(2026, 9, 10),
        description="Description",
    )


@pytest.mark.parametrize(
    "url",
    (
        "https://jobs.lever.co/toptal/5e236599-9746-4e95-94c5-f405138dcbd7",
        "https://jobs.lever.co/jobgether/634306c8-853d-4737-b74c-fbc4652cbaa1",
    ),
)
def test_rejects_aggregator_listings(url: str) -> None:
    decision = structural_filter(_job(url=url))

    assert decision.kind == "rejected"
    assert "Aggregator" in decision.reason


@pytest.mark.parametrize(
    "url",
    (
        "https://apply.workable.com/walletconnect/",
        "https://jobs.lever.co/acme",
        "https://boards.greenhouse.io/acme/",
        "https://boards.eu.greenhouse.io/acme/",
        "https://jobs.ashbyhq.com/acme",
    ),
)
def test_rejects_careers_index_pages(url: str) -> None:
    decision = structural_filter(_job(url=url))

    assert decision.kind == "rejected"
    assert "Careers-index" in decision.reason


@pytest.mark.parametrize(
    "title",
    (
        "General Application",
        "Ethena Labs - Join the Team! General Application",
        "Talent Pool",
        "Talent Community",
        "Future Opportunities",
        "Open Application",
        "Join Our Talent Network",
        "Join the Team",
    ),
)
def test_rejects_generic_titles(title: str) -> None:
    decision = structural_filter(_job(title=title))

    assert decision.kind == "rejected"
    assert "Generic" in decision.reason


def test_passes_an_ordinary_direct_employer_listing() -> None:
    assert structural_filter(_job()).kind == "pass"
    assert structural_filter(_job(title="Senior Engineer, General AI Platform")).kind == "pass"


def test_does_not_reject_an_aggregator_name_on_an_unrelated_host() -> None:
    job = _job(url="https://example.com/jobs.lever.co/jobgether/role")

    assert structural_filter(job).kind == "pass"
