from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from job_finder.ats import (
    AtsAvailable,
    AtsNotApplicable,
    AtsUnavailable,
    JsonHttpResponse,
    ats_structural_filter,
    detect_ats_source,
    fetch_ats_data,
    format_ats_block,
    parse_ashby_job,
    parse_ashby_url,
    parse_greenhouse_job,
    parse_greenhouse_url,
    parse_lever_job,
    parse_lever_url,
    parse_workable_job,
    parse_workable_url,
)

FIXTURES = Path(__file__).parents[2] / "fixtures/ats"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _fixture(name: str) -> JsonValue:
    return _JSON.validate_python(json.loads((FIXTURES / name).read_text()))


def test_parses_recorded_provider_payloads() -> None:
    ashby = parse_ashby_job(
        _fixture("ashby-ledger-org.json"),
        "4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
    )
    lever = parse_lever_job(_fixture("lever-yuno-platform-engineer-ai.json"))
    greenhouse = parse_greenhouse_job(_fixture("greenhouse-openup-senior-ai-engineer.json"))
    workable = parse_workable_job(_fixture("workable-v2-ai-listing.json"), "CF51DE915D")

    assert ashby == AtsAvailable(
        source="ashby",
        location="Paris, France",
        locations=("Paris, France",),
        workplace_type="OnSite",
        country="France, Metropolitan",
    )
    assert lever == AtsAvailable(
        source="lever",
        location="Argentina",
        locations=(
            "Argentina",
            "Bogota",
            "Chile",
            "Mexico",
            "Colombia",
            "Buenos Aires",
            "Europe",
            "Lima",
            "Paraguay",
            "Spain",
            "Amsterdam",
            "Belgium",
            "Brazil",
            "Germany",
            "Italy",
        ),
        workplace_type="Remote",
        country="AR",
    )
    assert greenhouse == AtsAvailable(
        source="greenhouse",
        location="Amsterdam",
        locations=("Amsterdam", "Amsterdam, North Holland, Netherlands"),
        workplace_type=None,
        country="Netherlands",
    )
    assert workable == AtsAvailable(
        source="workable",
        location="Sydney, Australia",
        locations=("Sydney, Australia",),
        workplace_type="Hybrid",
        country="Australia",
    )


@pytest.mark.parametrize(
    ("parser", "url", "expected"),
    (
        (parse_ashby_url, "https://jobs.ashbyhq.com/ledger/abc-123", ("ledger", "abc-123")),
        (parse_lever_url, "https://jobs.lever.co/yuno/abc-123", ("yuno", "abc-123")),
        (
            parse_greenhouse_url,
            "https://job-boards.greenhouse.io/openup/jobs/4847917101",
            ("openup", "4847917101"),
        ),
        (
            parse_workable_url,
            "https://apply.workable.com/v2-ai/j/CF51DE915D/",
            ("v2-ai", "CF51DE915D"),
        ),
    ),
)
def test_parses_provider_urls(
    parser: Callable[[str], tuple[str, str] | None],
    url: str,
    expected: tuple[str, str],
) -> None:
    assert parser(url) == expected


def test_rejects_invalid_provider_payloads() -> None:
    with pytest.raises(ValidationError):
        parse_greenhouse_job({"location": {"name": "Remote"}})
    with pytest.raises(ValidationError):
        parse_greenhouse_job({"id": "1", "location": {"name": "Remote"}})


def test_models_unavailable_and_unsupported_sources_explicitly() -> None:
    assert AtsUnavailable(source="lever", error_code="http_503").kind == "unavailable"
    assert AtsNotApplicable().kind == "not_applicable"
    assert detect_ats_source("https://example.com/job") is None
    assert detect_ats_source("https://lever.co.evil.example/job") is None
    assert detect_ats_source("https://evil.lever.co/job") is None
    assert detect_ats_source("https://example.com/?next=jobs.lever.co/x/y") is None


def test_rejects_only_explicit_on_site_evidence() -> None:
    onsite = AtsAvailable(
        source="ashby",
        location="New York",
        locations=("New York",),
        workplace_type="OnSite",
        country="United States",
    )
    unavailable = AtsUnavailable(source="ashby", error_code="network")

    decision = ats_structural_filter(onsite)
    assert decision.kind == "rejected"
    assert "New York" in decision.reason
    assert ats_structural_filter(unavailable).kind == "pass"


def test_formats_structured_evidence_for_the_evaluator() -> None:
    data = AtsAvailable(
        source="lever",
        location="Argentina",
        locations=("Argentina", "Europe", "Spain"),
        workplace_type="Remote",
        country="AR",
    )

    assert format_ats_block(data) == "\n".join(
        (
            "## ATS Structured Data (from lever API)",
            "- Primary location: Argentina",
            "- All listed locations: Argentina, Europe, Spain",
            "- Workplace type: Remote",
            "- Country fallback when locations are non-geographic: AR",
            "---",
        )
    )


def test_preserves_provider_specific_workplace_values() -> None:
    lever = parse_lever_job({"workplaceType": "on_site"})
    workable = parse_workable_job(
        {"results": [{"shortcode": "X", "workplace": "on_site"}]},
        "X",
    )

    assert lever.workplace_type is None
    assert workable is not None
    assert workable.workplace_type == "OnSite"


def test_dispatches_requests_and_reuses_the_ashby_org_response() -> None:
    requests_seen: list[tuple[str, str, dict[str, str] | None]] = []

    def fetch(method: str, url: str, body: dict[str, str] | None) -> JsonHttpResponse:
        requests_seen.append((method, url, body))
        return JsonHttpResponse(status_code=200, payload=_fixture("ashby-ledger-org.json"))

    cache: dict[str, JsonValue] = {}
    first = fetch_ats_data(
        "https://jobs.ashbyhq.com/ledger/4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
        fetch_json=fetch,
        ashby_cache=cache,
    )
    second = fetch_ats_data(
        "https://jobs.ashbyhq.com/ledger/missing",
        fetch_json=fetch,
        ashby_cache=cache,
    )

    assert first.kind == "available"
    assert second == AtsUnavailable(source="ashby", error_code="job_not_found")
    assert requests_seen == [
        (
            "GET",
            "https://api.ashbyhq.com/posting-api/job-board/ledger",
            None,
        )
    ]


def test_posts_the_title_to_workable() -> None:
    request: list[tuple[str, str, dict[str, str] | None]] = []

    def fetch(method: str, url: str, body: dict[str, str] | None) -> JsonHttpResponse:
        request.append((method, url, body))
        return JsonHttpResponse(status_code=200, payload=_fixture("workable-v2-ai-listing.json"))

    evidence = fetch_ats_data(
        "https://apply.workable.com/v2-ai/j/CF51DE915D/",
        title="Full Stack AI Principal Engineer",
        fetch_json=fetch,
    )

    assert evidence.kind == "available"
    assert request == [
        (
            "POST",
            "https://apply.workable.com/api/v3/accounts/v2-ai/jobs",
            {"query": "Full Stack AI Principal Engineer"},
        )
    ]


@pytest.mark.parametrize(
    ("url", "fixture", "expected_request"),
    (
        (
            "https://jobs.lever.co/yuno/33309adb-efb0-414c-9e9a-da13435a0242",
            "lever-yuno-platform-engineer-ai.json",
            (
                "GET",
                "https://api.lever.co/v0/postings/yuno/33309adb-efb0-414c-9e9a-da13435a0242?mode=json",
                None,
            ),
        ),
        (
            "https://boards.greenhouse.io/openup/jobs/4847917101",
            "greenhouse-openup-senior-ai-engineer.json",
            (
                "GET",
                "https://boards-api.greenhouse.io/v1/boards/openup/jobs/4847917101",
                None,
            ),
        ),
    ),
)
def test_dispatches_provider_get_requests(
    url: str,
    fixture: str,
    expected_request: tuple[str, str, dict[str, str] | None],
) -> None:
    requests_seen: list[tuple[str, str, dict[str, str] | None]] = []

    def fetch(method: str, target: str, body: dict[str, str] | None) -> JsonHttpResponse:
        requests_seen.append((method, target, body))
        return JsonHttpResponse(status_code=200, payload=_fixture(fixture))

    assert fetch_ats_data(url, fetch_json=fetch).kind == "available"
    assert requests_seen == [expected_request]


def test_returns_explicit_unavailable_evidence() -> None:
    def fetch(_method: str, _url: str, _body: dict[str, str] | None) -> JsonHttpResponse:
        return JsonHttpResponse(status_code=503, payload={})

    assert fetch_ats_data("https://jobs.lever.co/x/y", fetch_json=fetch) == AtsUnavailable(
        source="lever",
        error_code="http_503",
    )
    assert fetch_ats_data("https://apply.workable.com/x/j/y", fetch_json=fetch) == AtsUnavailable(
        source="workable",
        error_code="missing_title",
    )


def test_does_not_cache_an_invalid_ashby_response() -> None:
    calls = 0

    def fetch(_method: str, _url: str, _body: dict[str, str] | None) -> JsonHttpResponse:
        nonlocal calls
        calls += 1
        return JsonHttpResponse(status_code=200, payload={"wrong": "shape"})

    cache: dict[str, JsonValue] = {}
    first = fetch_ats_data(
        "https://jobs.ashbyhq.com/example/job-1",
        fetch_json=fetch,
        ashby_cache=cache,
    )
    second = fetch_ats_data(
        "https://jobs.ashbyhq.com/example/job-2",
        fetch_json=fetch,
        ashby_cache=cache,
    )

    assert first == AtsUnavailable(source="ashby", error_code="invalid_payload")
    assert second == first
    assert calls == 2
    assert cache == {}
