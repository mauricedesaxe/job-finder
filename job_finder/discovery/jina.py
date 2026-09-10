from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_finder.urls import parse_http_url

SEARCH_URL = "https://s.jina.ai/"
READER_URL = "https://r.jina.ai/"
_RETRYABLE_STATUSES = frozenset((429, 500, 502, 503))


class JinaModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")


class JinaSearchItem(JinaModel):
    title: str
    url: str
    description: str = ""


class JinaSearchEnvelope(JinaModel):
    code: int
    data: tuple[JinaSearchItem, ...]


class JinaReaderData(JinaModel):
    content: str


class JinaReaderEnvelope(JinaModel):
    code: int
    data: JinaReaderData


class SearchSucceeded(JinaModel):
    kind: Literal["succeeded"] = "succeeded"
    urls: tuple[str, ...]


class ScrapeSucceeded(JinaModel):
    kind: Literal["succeeded"] = "succeeded"
    markdown: str


class JinaUnavailable(JinaModel):
    kind: Literal["unavailable"] = "unavailable"
    operation: Literal["search", "scrape"]
    error_code: str
    reason: str


SearchResult = Annotated[SearchSucceeded | JinaUnavailable, Field(discriminator="kind")]
ScrapeResult = Annotated[ScrapeSucceeded | JinaUnavailable, Field(discriminator="kind")]


@dataclass(frozen=True)
class JinaHttpResponse:
    status_code: int
    body: str


JinaSender = Callable[[str, Mapping[str, str], dict[str, str], float], JinaHttpResponse]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class JinaRetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("Jina retry policy requires at least one attempt")
        if self.base_delay_seconds < 0:
            raise ValueError("Jina retry delay cannot be negative")


def build_search_query(keyword: str, domain: str) -> str:
    return f"site:{domain} {keyword}"


def search_jobs(
    keyword: str,
    domain: str,
    *,
    api_key: str,
    sender: JinaSender | None = None,
    retry_policy: JinaRetryPolicy | None = None,
    sleep: Sleeper = time.sleep,
) -> SearchResult:
    response = _request(
        SEARCH_URL,
        {"q": build_search_query(keyword, domain)},
        api_key,
        sender or send_jina_request,
        retry_policy or JinaRetryPolicy(),
        sleep,
        "search",
    )
    if isinstance(response, JinaUnavailable):
        return response
    try:
        envelope = JinaSearchEnvelope.model_validate_json(response.body)
    except ValidationError as error:
        return JinaUnavailable(operation="search", error_code="invalid_response", reason=str(error))
    if envelope.code != 200:
        return JinaUnavailable(
            operation="search",
            error_code=f"api_{envelope.code}",
            reason=f"Jina search returned code {envelope.code}",
        )
    return SearchSucceeded(urls=filter_job_urls(envelope.data, domain))


def scrape_job(
    url: str,
    *,
    api_key: str,
    sender: JinaSender | None = None,
    retry_policy: JinaRetryPolicy | None = None,
    sleep: Sleeper = time.sleep,
) -> ScrapeResult:
    response = _request(
        READER_URL,
        {"url": url},
        api_key,
        sender or send_jina_request,
        retry_policy or JinaRetryPolicy(),
        sleep,
        "scrape",
    )
    if isinstance(response, JinaUnavailable):
        return response
    try:
        envelope = JinaReaderEnvelope.model_validate_json(response.body)
    except ValidationError as error:
        return JinaUnavailable(operation="scrape", error_code="invalid_response", reason=str(error))
    if envelope.code != 200:
        return JinaUnavailable(
            operation="scrape",
            error_code=f"api_{envelope.code}",
            reason=f"Jina reader returned code {envelope.code}",
        )
    return ScrapeSucceeded(markdown=envelope.data.content)


def filter_job_urls(results: tuple[JinaSearchItem, ...], domain: str) -> tuple[str, ...]:
    seen: set[str] = set()
    urls: list[str] = []
    for result in results:
        url = result.url.rstrip(".,;:!?")
        parsed = parse_http_url(url)
        if parsed is None or parsed.hostname != domain or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return tuple(urls)


def send_jina_request(
    url: str,
    headers: Mapping[str, str],
    body: dict[str, str],
    timeout_seconds: float,
) -> JinaHttpResponse:
    response = requests.post(url, headers=headers, json=body, timeout=timeout_seconds)
    return JinaHttpResponse(status_code=response.status_code, body=response.text)


def _request(
    url: str,
    body: dict[str, str],
    api_key: str,
    sender: JinaSender,
    policy: JinaRetryPolicy,
    sleep: Sleeper,
    operation: Literal["search", "scrape"],
) -> JinaHttpResponse | JinaUnavailable:
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    for attempt in range(policy.max_attempts):
        try:
            response = sender(url, headers, body, 30.0)
        except requests.RequestException as error:
            if attempt + 1 == policy.max_attempts:
                return JinaUnavailable(
                    operation=operation,
                    error_code="network_error",
                    reason=str(error),
                )
        else:
            if response.status_code == 200:
                return response
            if response.status_code not in _RETRYABLE_STATUSES:
                return JinaUnavailable(
                    operation=operation,
                    error_code=f"http_{response.status_code}",
                    reason=f"Jina returned HTTP {response.status_code}",
                )
            if attempt + 1 == policy.max_attempts:
                return JinaUnavailable(
                    operation=operation,
                    error_code=f"http_{response.status_code}",
                    reason=f"Jina returned HTTP {response.status_code}",
                )
        sleep(policy.base_delay_seconds * 2.0**attempt)
    raise AssertionError("A valid Jina retry policy always returns")
