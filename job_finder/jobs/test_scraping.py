from datetime import date
import json
from pathlib import Path

from pydantic import BaseModel

from job_finder.jobs.scraping import (
    detect_source,
    extract_company_from_url,
    extract_date_posted,
    extract_title,
    parse_job_details,
)


def test_extracts_company_from_supported_job_board_urls() -> None:
    assert extract_company_from_url("https://jobs.ashbyhq.com/acme/12345") == "acme"
    assert extract_company_from_url("https://jobs.lever.co/mycompany/abc-def") == "mycompany"
    assert extract_company_from_url("https://boards.greenhouse.io/coolstartup/jobs/123") == (
        "coolstartup"
    )
    assert extract_company_from_url("https://apply.workable.com/mlabs/j/C07B32BD46") == "mlabs"
    assert extract_company_from_url("not-a-url") == "Unknown"
    assert extract_company_from_url("https://jobs.ashbyhq.com/") == "Unknown"


def test_detects_the_job_source() -> None:
    assert detect_source("https://jobs.ashbyhq.com/acme/123") == "ashbyhq"
    assert detect_source("https://jobs.lever.co/company/abc") == "lever"
    assert detect_source("https://boards.greenhouse.io/co/jobs/1") == "greenhouse"
    assert detect_source("https://apply.workable.com/mlabs/j/C07B32BD46") == "workable"
    assert detect_source("https://example.com/jobs/1") == "other"


def test_extracts_titles_in_precedence_order() -> None:
    assert extract_title("Title: Canonical\n\n# Heading\n\n**Bold**") == "Canonical"
    assert extract_title("# Heading\n\n**Bold**") == "Heading"
    assert extract_title("Page copy\n\n**Bold**") == "Bold"
    assert extract_title("Page copy") == "Unknown Position"


def test_extracts_supported_posted_dates() -> None:
    assert extract_date_posted("Posted on January 15, 2025") == date(2025, 1, 15)
    assert extract_date_posted("Published: 2025-03-20") == date(2025, 3, 20)
    assert extract_date_posted("Date: 3/20/2025") == date(2025, 3, 20)
    assert extract_date_posted("Posted on Jan 15, 2025") == date(2025, 1, 15)
    assert extract_date_posted("No date information") is None


def test_parses_a_complete_job_listing() -> None:
    markdown = "# DeFi Protocol Engineer\n\nPosted on March 1, 2025\n\nBuild stuff."

    job = parse_job_details(
        markdown,
        "https://jobs.ashbyhq.com/acme/12345",
        "defi",
        scraped_on=date(2026, 9, 10),
    )

    assert job.title == "DeFi Protocol Engineer"
    assert job.company == "acme"
    assert job.source == "ashbyhq"
    assert job.keywords_matched == ("defi",)
    assert job.date_posted == date(2025, 3, 1)
    assert job.date_scraped == date(2026, 9, 10)
    assert job.description == markdown


def test_does_not_detect_a_source_from_query_text() -> None:
    assert detect_source("https://example.com/job?next=jobs.lever.co/company/id") == "other"


def test_prefers_the_reader_page_title_over_markdown_heuristics() -> None:
    job = parse_job_details(
        "**About CaptivateIQ**\n\nWe are the leading platform.",
        "https://jobs.lever.co/captivateiq/25b5dbc3",
        "ai platform",
        scraped_on=date(2026, 9, 12),
        page_title="CaptivateIQ - Staff Software Engineer - AI Platform",
    )

    assert job.title == "CaptivateIQ - Staff Software Engineer - AI Platform"


def test_falls_back_to_markdown_when_the_reader_title_is_empty() -> None:
    job = parse_job_details(
        "# DeFi Protocol Engineer\n\nBuild stuff.",
        "https://jobs.ashbyhq.com/acme/12345",
        "defi",
        scraped_on=date(2026, 9, 12),
        page_title="   ",
    )

    assert job.title == "DeFi Protocol Engineer"


class _ReaderPage(BaseModel):
    title: str = ""
    url: str = ""
    content: str = ""


def test_recovers_the_real_title_for_a_recorded_mis_titled_listing() -> None:
    envelope = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "fixtures/jina/reader_captivateiq_staff_software_engineer.json"
        ).read_text()
    )
    page = _ReaderPage.model_validate(envelope["data"])

    job = parse_job_details(
        page.content,
        page.url,
        "ai platform",
        scraped_on=date(2026, 9, 12),
        page_title=page.title,
    )

    assert job.title == "CaptivateIQ - Staff Software Engineer - AI Platform"
    assert job.title != "About CaptivateIQ"
    assert job.source == "lever"


def test_rejects_malformed_urls() -> None:
    assert extract_company_from_url("https://[bad") == "Unknown"
    assert extract_company_from_url("https://example.com:bad/job") == "Unknown"
