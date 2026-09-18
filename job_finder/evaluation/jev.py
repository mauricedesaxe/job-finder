from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from job_finder.evaluation.models import (
    CriterionAccepted,
    RetryableOperationalError,
    TerminalOperationalError,
)
from job_finder.evaluation.prompt_releases import PromptVersion
from job_finder.evaluation.prompts import EVALUATION_PROMPTS

JEV_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
JEV_PASS_THRESHOLD = 0.5
JEV_INPUT_COST_PER_MILLION = Decimal("0.042")
_RETRYABLE_HTTP_STATUSES = frozenset((408, 429, *range(500, 600)))


class JevModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class JevNoulCriteria(JevModel):
    true: str
    false: str


class JevNoulQuestion(JevModel):
    type: Literal["noul"] = "noul"
    instructions: str = Field(min_length=1)
    criteria: JevNoulCriteria


class JevNoulQuestionTemplate(JevModel):
    instructions: str = Field(min_length=1)
    criteria: JevNoulCriteria

    def render(self, values: Mapping[str, str]) -> JevNoulQuestion:
        return JevNoulQuestion(
            instructions=self.instructions.format_map(values),
            criteria=self.criteria,
        )


class JevSystemOneRequest(JevModel):
    state: str
    model: Literal["jev-1.13.0"] = JEV_MODEL
    questions: dict[str, JevNoulQuestion] = Field(min_length=1, max_length=1)


class JevNoulAnswer(JevModel):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class JevUsage(JevModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class JevSystemOneResponse(JevModel):
    model: Literal["jev-1.13.0"]
    answers: dict[str, JevNoulAnswer] = Field(min_length=1, max_length=1)
    usage: JevUsage


class JevCriterionObservation(JevModel):
    result: CriterionAccepted
    provider_request_id: str | None = None
    pass_probability: float = Field(ge=0, le=1)
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)


class JevRunMetrics(JevModel):
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    p50_latency_ms: float | None
    p95_latency_ms: float | None

    @model_validator(mode="after")
    def latency_presence_matches_requests(self) -> JevRunMetrics:
        has_p50 = self.p50_latency_ms is not None
        has_p95 = self.p95_latency_ms is not None
        if has_p50 != has_p95:
            raise ValueError("Latency percentiles must both be present or absent")
        has_latency = has_p50 and has_p95
        if has_latency != (self.request_count > 0):
            raise ValueError("Latency percentiles require at least one request")
        return self


class JevHttpResponse(JevModel):
    status_code: int
    body: str
    provider_request_id: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)


@dataclass(frozen=True)
class JevRetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("Jev retry policy requires at least one attempt")
        if self.base_delay_seconds < 0:
            raise ValueError("Jev retry delay cannot be negative")


JevSender = Callable[[str, Mapping[str, str], dict[str, object], float], JevHttpResponse]
JevCriterionResult = JevCriterionObservation | RetryableOperationalError | TerminalOperationalError
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
RequestObserver = Callable[[int], None]


def _question_template(rubric: str) -> JevNoulQuestionTemplate:
    return JevNoulQuestionTemplate(
        instructions=(
            "Does this job listing pass the following evaluation criterion? "
            "Apply its PASS and FAIL rules exactly.\n\n"
            f"{rubric}"
        ),
        criteria=JevNoulCriteria(
            true="The listing passes the criterion according to its PASS and FAIL rules.",
            false="The listing fails the criterion according to its PASS and FAIL rules.",
        ),
    )


JEV_QUESTIONS: Mapping[str, JevNoulQuestionTemplate] = MappingProxyType(
    {prompt.criterion: _question_template(prompt.system_message) for prompt in EVALUATION_PROMPTS}
)


def jev_policy_digest(rates: str) -> str:
    payload = {
        "model": JEV_MODEL,
        "pass_threshold": JEV_PASS_THRESHOLD,
        "rates": rates,
        "questions": {
            criterion: question.model_dump(mode="json")
            for criterion, question in JEV_QUESTIONS.items()
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def evaluate_prompt(
    prompt: PromptVersion,
    values: Mapping[str, str],
    *,
    api_key: str,
    sender: JevSender | None = None,
    retry_policy: JevRetryPolicy | None = None,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    observe_request: RequestObserver | None = None,
) -> JevCriterionResult:
    expected_inputs = set(prompt.definition.inputs)
    if set(values) != expected_inputs:
        raise ValueError(
            f"Prompt {prompt.definition.name} requires inputs {sorted(expected_inputs)}"
        )
    template = JEV_QUESTIONS.get(prompt.definition.criterion)
    if template is None:
        raise ValueError(f"No Jev question registered for {prompt.definition.criterion}")
    question = template.render(values)
    request = JevSystemOneRequest(
        state=values["job"],
        questions={prompt.definition.criterion: question},
    )
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    policy = retry_policy or JevRetryPolicy()
    send = sender or send_system_one
    for attempt in range(policy.max_attempts):
        retry_after_seconds: float | None = None
        started_at = clock()
        try:
            response = send(
                JEV_SYSTEM_ONE_URL,
                headers,
                request.model_dump(mode="json"),
                30.0,
            )
        except requests.RequestException as error:
            latency_ms = max(0, round((clock() - started_at) * 1000))
            if observe_request is not None:
                observe_request(latency_ms)
            failure: RetryableOperationalError | TerminalOperationalError = (
                RetryableOperationalError(
                    prompt_name=prompt.definition.name,
                    error_code="network_error",
                    reason=str(error),
                )
            )
        else:
            latency_ms = max(0, round((clock() - started_at) * 1000))
            if observe_request is not None:
                observe_request(latency_ms)
            if response.status_code == 200:
                break
            retry_after_seconds = response.retry_after_seconds
            error_type = (
                RetryableOperationalError
                if response.status_code in _RETRYABLE_HTTP_STATUSES
                else TerminalOperationalError
            )
            failure = error_type(
                prompt_name=prompt.definition.name,
                error_code=f"http_{response.status_code}",
                reason=f"Jev returned HTTP {response.status_code}",
            )
        if not isinstance(failure, RetryableOperationalError) or attempt + 1 >= policy.max_attempts:
            return failure
        sleep(
            retry_after_seconds
            if retry_after_seconds is not None
            else policy.base_delay_seconds * 2.0**attempt
        )
    else:
        raise AssertionError("A valid Jev retry policy always returns or receives a response")
    try:
        parsed = JevSystemOneResponse.model_validate_json(response.body)
    except ValidationError as error:
        return TerminalOperationalError(
            prompt_name=prompt.definition.name,
            error_code="invalid_response",
            reason=str(error),
        )
    criterion = prompt.definition.criterion
    if set(parsed.answers) != {criterion}:
        return TerminalOperationalError(
            prompt_name=prompt.definition.name,
            error_code="invalid_response",
            reason=f"Jev response did not contain exactly the {criterion} answer",
        )
    probability = parsed.answers[criterion].noul
    passed = probability >= JEV_PASS_THRESHOLD
    comparison = "met" if passed else "was below"
    accepted = CriterionAccepted(
        prompt_name=prompt.definition.name,
        passed=passed,
        reason=(
            f"Jev pass probability {probability:.3f} {comparison} "
            f"threshold {JEV_PASS_THRESHOLD:.3f}."
        ),
    )
    return JevCriterionObservation(
        result=accepted,
        provider_request_id=response.provider_request_id,
        pass_probability=probability,
        model=parsed.model,
        input_tokens=parsed.usage.input_tokens,
        output_tokens=parsed.usage.output_tokens,
        latency_ms=latency_ms,
        estimated_cost_usd=(
            Decimal(parsed.usage.input_tokens) * JEV_INPUT_COST_PER_MILLION / Decimal(1_000_000)
        ),
    )


def send_system_one(
    url: str,
    headers: Mapping[str, str],
    body: dict[str, object],
    timeout_seconds: float,
) -> JevHttpResponse:
    response = requests.post(url, headers=headers, json=body, timeout=timeout_seconds)
    return JevHttpResponse(
        status_code=response.status_code,
        body=response.text,
        provider_request_id=response.headers.get("x-typesafe-request-id"),
        retry_after_seconds=_retry_after_seconds(response.headers),
    )


def summarize_observations(
    observations: Sequence[JevCriterionObservation],
    request_latencies_ms: Sequence[int] | None = None,
) -> JevRunMetrics:
    latencies = sorted(
        request_latencies_ms
        if request_latencies_ms is not None
        else (observation.latency_ms for observation in observations)
    )
    return JevRunMetrics(
        request_count=len(latencies),
        input_tokens=sum(observation.input_tokens for observation in observations),
        output_tokens=sum(observation.output_tokens for observation in observations),
        estimated_cost_usd=sum(
            (observation.estimated_cost_usd for observation in observations),
            start=Decimal(0),
        ),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
    )


def _percentile(sorted_values: Sequence[int], percentile: float) -> float | None:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * percentile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - lower_index
    return (
        sorted_values[lower_index]
        + (sorted_values[upper_index] - sorted_values[lower_index]) * fraction
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    milliseconds = headers.get("retry-after-ms")
    seconds = headers.get("retry-after")
    try:
        if milliseconds is not None:
            return max(0.0, float(milliseconds) / 1000)
        if seconds is not None:
            return max(0.0, float(seconds))
    except ValueError:
        return None
    return None
