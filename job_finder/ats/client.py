from __future__ import annotations

import json
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass

import requests
from pydantic import JsonValue, TypeAdapter, ValidationError

from job_finder.ats.ashby import parse_ashby_job, parse_ashby_url
from job_finder.ats.greenhouse import parse_greenhouse_job, parse_greenhouse_url
from job_finder.ats.lever import parse_lever_job, parse_lever_url
from job_finder.ats.models import AtsEvidence, AtsNotApplicable, AtsUnavailable
from job_finder.ats.policy import detect_ats_source
from job_finder.ats.workable import parse_workable_job, parse_workable_url

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True)
class JsonHttpResponse:
    status_code: int
    payload: JsonValue


JsonFetcher = Callable[[str, str, dict[str, str] | None], JsonHttpResponse]


def fetch_ats_data(
    url: str,
    *,
    title: str | None = None,
    fetch_json: JsonFetcher | None = None,
    ashby_cache: MutableMapping[str, JsonValue] | None = None,
) -> AtsEvidence:
    source = detect_ats_source(url)
    if source is None:
        return AtsNotApplicable()
    fetch = fetch_json or _fetch_json
    try:
        if source == "lever":
            parsed = parse_lever_url(url)
            if parsed is None:
                return AtsUnavailable(source=source, error_code="invalid_url")
            org, job_id = parsed
            response = fetch(
                "GET",
                f"https://api.lever.co/v0/postings/{org}/{job_id}?mode=json",
                None,
            )
            if response.status_code != 200:
                return AtsUnavailable(source=source, error_code=f"http_{response.status_code}")
            return parse_lever_job(response.payload)
        if source == "ashby":
            return _fetch_ashby(url, fetch, ashby_cache if ashby_cache is not None else {})
        if source == "greenhouse":
            parsed = parse_greenhouse_url(url)
            if parsed is None:
                return AtsUnavailable(source=source, error_code="invalid_url")
            org, job_id = parsed
            response = fetch(
                "GET",
                f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job_id}",
                None,
            )
            if response.status_code != 200:
                return AtsUnavailable(source=source, error_code=f"http_{response.status_code}")
            return parse_greenhouse_job(response.payload)
        if title is None:
            return AtsUnavailable(source=source, error_code="missing_title")
        parsed = parse_workable_url(url)
        if parsed is None:
            return AtsUnavailable(source=source, error_code="invalid_url")
        slug, shortcode = parsed
        response = fetch(
            "POST",
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            {"query": title},
        )
        if response.status_code != 200:
            return AtsUnavailable(source=source, error_code=f"http_{response.status_code}")
        result = parse_workable_job(response.payload, shortcode)
        return result or AtsUnavailable(source=source, error_code="job_not_found")
    except (requests.RequestException, json.JSONDecodeError):
        return AtsUnavailable(source=source, error_code="network_or_json")
    except ValidationError:
        return AtsUnavailable(source=source, error_code="invalid_payload")


def _fetch_ashby(
    url: str,
    fetch: JsonFetcher,
    cache: MutableMapping[str, JsonValue],
) -> AtsEvidence:
    parsed = parse_ashby_url(url)
    if parsed is None:
        return AtsUnavailable(source="ashby", error_code="invalid_url")
    org, job_id = parsed
    payload = cache.get(org)
    if payload is None:
        response = fetch(
            "GET",
            f"https://api.ashbyhq.com/posting-api/job-board/{org}",
            None,
        )
        if response.status_code != 200:
            return AtsUnavailable(source="ashby", error_code=f"http_{response.status_code}")
        payload = response.payload
        result = parse_ashby_job(payload, job_id)
        cache[org] = payload
        return result or AtsUnavailable(source="ashby", error_code="job_not_found")
    result = parse_ashby_job(payload, job_id)
    return result or AtsUnavailable(source="ashby", error_code="job_not_found")


def _fetch_json(method: str, url: str, json_body: dict[str, str] | None) -> JsonHttpResponse:
    response = requests.request(method, url, json=json_body, timeout=30)
    return JsonHttpResponse(
        status_code=response.status_code,
        payload=_JSON.validate_json(response.text),
    )
