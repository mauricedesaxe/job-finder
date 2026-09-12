from __future__ import annotations

import re
from datetime import date, datetime
from job_finder.jobs.models import JobListing, JobSource
from job_finder.urls import parse_http_url

_JINA_TITLE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
_HEADING_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_BOLD_TITLE = re.compile(r"\*\*(.+?)\*\*")
_POSTED_DATE_PATTERNS = (
    re.compile(r"(?:posted|published|date)\s*(?:on|:)?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.I),
    re.compile(r"(?:posted|published|date)\s*(?:on|:)?\s*(\d{4}-\d{2}-\d{2})", re.I),
    re.compile(r"(?:posted|published|date)\s*(?:on|:)?\s*(\d{1,2}/\d{1,2}/\d{4})", re.I),
)
_DATE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
)


def parse_job_details(
    markdown: str,
    url: str,
    keyword: str,
    *,
    scraped_on: date,
    page_title: str = "",
) -> JobListing:
    return JobListing(
        title=page_title.strip() or extract_title(markdown),
        company=extract_company_from_url(url),
        url=url,
        source=detect_source(url),
        keywords_matched=(keyword,),
        date_posted=extract_date_posted(markdown),
        date_scraped=scraped_on,
        description=markdown[:8000],
    )


def extract_company_from_url(url: str) -> str:
    parsed = parse_http_url(url)
    if parsed is None:
        return "Unknown"
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    return segments[0] if segments else "Unknown"


def detect_source(url: str) -> JobSource:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return "other"
    if parsed.hostname == "jobs.ashbyhq.com":
        return "ashbyhq"
    if parsed.hostname == "jobs.lever.co":
        return "lever"
    if parsed.hostname in (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
    ):
        return "greenhouse"
    if parsed.hostname == "apply.workable.com":
        return "workable"
    return "other"


def extract_title(markdown: str) -> str:
    for pattern in (_JINA_TITLE, _HEADING_TITLE, _BOLD_TITLE):
        match = pattern.search(markdown)
        if match is not None:
            return match.group(1).strip()
    return "Unknown Position"


def extract_date_posted(markdown: str) -> date | None:
    for pattern in _POSTED_DATE_PATTERNS:
        match = pattern.search(markdown)
        if match is None:
            continue
        value = match.group(1)
        for date_format in _DATE_FORMATS:
            try:
                return datetime.strptime(value, date_format).date()
            except ValueError:
                continue
    return None
