import json
from collections.abc import Mapping

import pytest

from job_finder.discovery.jina import (
    JinaHttpResponse,
    JinaRetryPolicy,
    JinaUnavailable,
    ScrapeSucceeded,
    SearchSucceeded,
    scrape_job,
    search_jobs,
)
from job_finder.pipeline.orchestration import DiscoverySummary


def test_rejects_an_incomplete_discovery_summary() -> None:
    summary = DiscoverySummary(
        query_count=4,
        unavailable_query_count=1,
        discovered_count=3,
        new_work_count=3,
    )

    with pytest.raises(RuntimeError, match="1 discovery queries"):
        summary.require_complete()


def test_searches_with_the_legacy_query_and_keeps_exact_domain_urls() -> None:
    requests: list[tuple[str, Mapping[str, str], dict[str, str]]] = []

    def send(
        url: str,
        headers: Mapping[str, str],
        body: dict[str, str],
        _timeout: float,
    ) -> JinaHttpResponse:
        requests.append((url, headers, body))
        return JinaHttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "code": 200,
                    "data": [
                        {"title": "A", "url": "https://jobs.lever.co/acme/1."},
                        {"title": "A again", "url": "https://jobs.lever.co/acme/1"},
                        {
                            "title": "Spoof",
                            "url": "https://attacker.example/jobs.lever.co/acme/2",
                        },
                    ],
                }
            ),
        )

    result = search_jobs("senior engineer", "jobs.lever.co", api_key="key", sender=send)

    assert isinstance(result, SearchSucceeded)
    assert result.urls == ("https://jobs.lever.co/acme/1",)
    assert requests[0][2] == {"q": "site:jobs.lever.co senior engineer"}
    assert requests[0][1]["accept"] == "application/json"
    assert requests[0][1]["authorization"] == "Bearer key"


def test_retries_reader_throttling_and_parses_markdown() -> None:
    attempts = 0
    delays: list[float] = []

    def send(
        _url: str,
        _headers: Mapping[str, str],
        body: dict[str, str],
        _timeout: float,
    ) -> JinaHttpResponse:
        nonlocal attempts
        attempts += 1
        assert body == {"url": "https://jobs.ashbyhq.com/acme/1"}
        if attempts == 1:
            return JinaHttpResponse(status_code=429, body="throttled")
        return JinaHttpResponse(
            status_code=200,
            body=json.dumps({"code": 200, "data": {"content": "# Senior Engineer"}}),
        )

    result = scrape_job(
        "https://jobs.ashbyhq.com/acme/1",
        api_key="key",
        sender=send,
        retry_policy=JinaRetryPolicy(max_attempts=2, base_delay_seconds=0.5),
        sleep=delays.append,
    )

    assert result == ScrapeSucceeded(markdown="# Senior Engineer")
    assert attempts == 2
    assert delays == [0.5]


def test_carries_the_reader_title_alongside_the_markdown() -> None:
    def send(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, str],
        _timeout: float,
    ) -> JinaHttpResponse:
        return JinaHttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "code": 200,
                    "data": {
                        "title": "CaptivateIQ - Staff Software Engineer - AI Platform",
                        "content": "**About CaptivateIQ**\n\nWe build things.",
                    },
                }
            ),
        )

    result = scrape_job("https://jobs.lever.co/captivateiq/abc", api_key="key", sender=send)

    assert result == ScrapeSucceeded(
        title="CaptivateIQ - Staff Software Engineer - AI Platform",
        markdown="**About CaptivateIQ**\n\nWe build things.",
    )


def test_returns_unavailable_for_an_invalid_reader_response() -> None:
    result = scrape_job(
        "https://jobs.ashbyhq.com/acme/1",
        api_key="key",
        sender=lambda _url, _headers, _body, _timeout: JinaHttpResponse(
            status_code=200, body='{"code":200,"data":{}}'
        ),
    )

    assert isinstance(result, JinaUnavailable)
    assert result.error_code == "invalid_response"
