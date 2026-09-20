from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, Literal
from uuid import uuid4

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from job_finder.evaluation.models import (
    CompletedModelCall,
    CriterionAccepted,
    CriterionResult,
    ModelCallAttempt,
    ModelCallContext,
    RetryableOperationalError,
    TerminalOperationalError,
)
from job_finder.evaluation.openrouter import (
    ModelCallPersistence,
    PendingModelCallUsage,
    model_request_id,
    prompt_input_digest,
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
    questions: dict[str, JevNoulQuestion] = Field(min_length=1)


class JevNoulAnswer(JevModel):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class JevUsage(JevModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class JevSystemOneResponse(JevModel):
    model: Literal["jev-1.13.0"]
    answers: dict[str, JevNoulAnswer] = Field(min_length=1)
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
RetryObserver = Callable[[RetryableOperationalError, int], None]
JevPolicy = Literal["faithful", "atomic"]


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


def _atomic_question(instructions: str) -> JevNoulQuestionTemplate:
    return JevNoulQuestionTemplate(
        instructions=instructions,
        criteria=JevNoulCriteria(
            true="The listing clearly contains this signal.",
            false="The signal is absent, ambiguous, or only a preference.",
        ),
    )


ATOMIC_QUESTIONS: Mapping[str, Mapping[str, JevNoulQuestionTemplate]] = MappingProxyType(
    {
        "remote-europe-eligible": MappingProxyType(
            {
                "requires_office_attendance": _atomic_question(
                    "Does this role require onsite or hybrid office attendance?"
                ),
                "remote_restricted_outside_europe": _atomic_question(
                    "Is remote work explicitly limited to a region that excludes Europe?"
                ),
                "requires_non_europe_residence": _atomic_question(
                    "Does the role require residence or work authorization in a country outside Europe?"
                ),
                "lower_compensation_market_skew": _atomic_question(
                    "Are the allowed locations dominated by lower-compensation markets with only one or two token higher-compensation European locations?"
                ),
            }
        ),
        "compensation-minimum": MappingProxyType(
            {
                "explicit_below_minimum_cash_compensation": _atomic_question(
                    "Does the listing state a concrete base-cash maximum below $130,000 per year, $65 per hour, or the equivalent after converting period and currency using these rates: {rates}? Ignore equity, bonus, and total compensation."
                )
            }
        ),
        "role-quality": MappingProxyType(
            {
                "enterprise_stack": _atomic_question(
                    "Is the primary stack Java/Spring, .NET/C#, Scala, C++, or Angular/Kendo?"
                ),
                "non_product_role": _atomic_question(
                    "Is this primarily architect-only, manager-only, sales, solutions, field, or customer-delivery work without substantial hands-on product engineering?"
                ),
                "data_plumbing": _atomic_question(
                    "Is the primary work data-warehouse or data-pipeline plumbing rather than product features?"
                ),
                "extreme_seniority": _atomic_question(
                    "Does a principal or distinguished role require at least ten years of experience?"
                ),
                "four_sync_interviews": _atomic_question(
                    "Does the listing disclose at least four synchronous interview rounds, excluding take-homes, references, application review, and offer?"
                ),
                "non_english_team": _atomic_question(
                    "Does the job body contain substantial non-English prose indicating a non-English-primary team?"
                ),
                "infrastructure_operations": _atomic_question(
                    "Is the primary work infrastructure operations such as clusters, deployments, observability, cost optimization, reliability, or incidents?"
                ),
                "core_blockchain_protocol": _atomic_question(
                    "Is the primary work blockchain consensus, cryptography, peer-to-peer networking, validator infrastructure, or core protocol development?"
                ),
            }
        ),
        "cheap-shop-placement": MappingProxyType(
            {
                "recruiter_for_client": _atomic_question(
                    "Does the listing recruit on behalf of a separate client?"
                ),
                "placement_business": _atomic_question(
                    "Does the listing entity describe itself as a placement, matching, staffing, or talent-connection service?"
                ),
                "recruiter_brand_title": _atomic_question(
                    "Does a recruiter brand prefix the title while the body confirms work for another company?"
                ),
                "low_seniority_bar": _atomic_question(
                    "Does a senior, lead, or founding role ask for only three total years, or five years with only one senior year?"
                ),
                "placement_without_compensation": _atomic_question(
                    "Is placement or client-engagement language present without concrete cash compensation?"
                ),
                "low_compensation_region": _atomic_question(
                    "Is the talent pool restricted to one lower-compensation region?"
                ),
                "low_code_tools": _atomic_question(
                    "Are n8n, Zapier, Make, Bubble, or similar low-code automation tools required or a strong plus?"
                ),
                "foreign_client_hours": _atomic_question(
                    "Does independent-contractor placement require overlap with a foreign client's business hours?"
                ),
            }
        ),
        "early-stage-product-engineer": MappingProxyType(
            {
                "owns_product_delivery": _atomic_question(
                    "Does the individual contributor substantially own shipping an MVP, customer product feature, user experience, or product-serving backend from idea through production?"
                ),
                "excluded_primary_shape": _atomic_question(
                    "Is the role primarily people management, infrastructure operations, internal platform work without product responsibility, pure architecture, sales, customer delivery, or narrow research?"
                ),
            }
        ),
        "applied-ai-product-engineer": MappingProxyType(
            {
                "ships_ai_product": _atomic_question(
                    "Does the role substantially own shipping customer-facing LLM, agent, RAG, evaluation, retrieval, tool-use, or other applied-AI product capabilities?"
                ),
                "excluded_ai_shape": _atomic_question(
                    "Is the primary work pure ML research or training, search engineering, data engineering, operational MLOps, model-serving operations, or internal AI tooling without a stated customer-product dependency?"
                ),
            }
        ),
    }
)


def jev_policy_digest(rates: str, policy: JevPolicy = "faithful") -> str:
    questions: Mapping[str, object]
    if policy == "faithful":
        questions = JEV_QUESTIONS
    else:
        questions = ATOMIC_QUESTIONS
    payload = {
        "model": JEV_MODEL,
        "pass_threshold": JEV_PASS_THRESHOLD,
        "rates": rates,
        "questions": {
            criterion: (
                question.model_dump(mode="json")
                if isinstance(question, JevNoulQuestionTemplate)
                else {name: template.model_dump(mode="json") for name, template in question.items()}
            )
            for criterion, question in questions.items()
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
    observe_retry: RetryObserver | None = None,
    policy: JevPolicy = "faithful",
) -> JevCriterionResult:
    expected_inputs = set(prompt.definition.inputs)
    if set(values) != expected_inputs:
        raise ValueError(
            f"Prompt {prompt.definition.name} requires inputs {sorted(expected_inputs)}"
        )
    criterion = prompt.definition.criterion
    if criterion not in JEV_QUESTIONS:
        raise ValueError(f"No Jev question registered for {prompt.definition.criterion}")
    questions = (
        {criterion: JEV_QUESTIONS[criterion].render(values)}
        if policy == "faithful"
        else {
            name: template.render(values) for name, template in ATOMIC_QUESTIONS[criterion].items()
        }
    )
    request = JevSystemOneRequest(
        state=values["job"],
        questions=questions,
    )
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    request_result = _send_with_retry(
        prompt.definition.name,
        request,
        headers,
        sender or send_system_one,
        retry_policy or JevRetryPolicy(),
        sleep,
        clock,
        observe_request,
        observe_retry,
    )
    if isinstance(request_result, RetryableOperationalError | TerminalOperationalError):
        return request_result
    response, latency_ms = request_result
    try:
        parsed = JevSystemOneResponse.model_validate_json(response.body)
    except ValidationError as error:
        return TerminalOperationalError(
            prompt_name=prompt.definition.name,
            error_code="invalid_response",
            reason=str(error),
        )
    if set(parsed.answers) != set(questions):
        return TerminalOperationalError(
            prompt_name=prompt.definition.name,
            error_code="invalid_response",
            reason="Jev response did not contain exactly the requested answers",
        )
    probabilities = {name: answer.noul for name, answer in parsed.answers.items()}
    if policy == "faithful":
        probability = probabilities[criterion]
        passed = probability >= JEV_PASS_THRESHOLD
        comparison = "met" if passed else "was below"
        reason = (
            f"Jev pass probability {probability:.3f} {comparison} "
            f"threshold {JEV_PASS_THRESHOLD:.3f}."
        )
    else:
        passed, probability = _compose_atomic(criterion, probabilities)
        signals = ", ".join(f"{name}={value:.3f}" for name, value in probabilities.items())
        reason = f"Jev atomic policy {'passed' if passed else 'failed'}: {signals}."
    accepted = CriterionAccepted(
        prompt_name=prompt.definition.name,
        passed=passed,
        reason=reason,
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


def _send_with_retry(
    prompt_name: str,
    request: JevSystemOneRequest,
    headers: Mapping[str, str],
    sender: JevSender,
    retry_policy: JevRetryPolicy,
    sleep: Sleeper,
    clock: Clock,
    observe_request: RequestObserver | None,
    observe_retry: RetryObserver | None,
) -> tuple[JevHttpResponse, int] | RetryableOperationalError | TerminalOperationalError:
    for attempt in range(retry_policy.max_attempts):
        retry_after_seconds: float | None = None
        started_at = clock()
        try:
            response = sender(
                JEV_SYSTEM_ONE_URL,
                headers,
                request.model_dump(mode="json"),
                30.0,
            )
        except requests.RequestException as error:
            latency_ms = max(0, round((clock() - started_at) * 1000))
            failure: RetryableOperationalError | TerminalOperationalError = (
                RetryableOperationalError(
                    prompt_name=prompt_name,
                    error_code="network_error",
                    reason=str(error),
                )
            )
        else:
            latency_ms = max(0, round((clock() - started_at) * 1000))
            if response.status_code == 200:
                if observe_request is not None:
                    observe_request(latency_ms)
                return response, latency_ms
            retry_after_seconds = response.retry_after_seconds
            error_type = (
                RetryableOperationalError
                if response.status_code in _RETRYABLE_HTTP_STATUSES
                else TerminalOperationalError
            )
            failure = error_type(
                prompt_name=prompt_name,
                error_code=f"http_{response.status_code}",
                reason=f"Jev returned HTTP {response.status_code}",
            )
        if observe_request is not None:
            observe_request(latency_ms)
        if (
            not isinstance(failure, RetryableOperationalError)
            or attempt + 1 >= retry_policy.max_attempts
        ):
            return failure
        if observe_retry is not None:
            observe_retry(failure, latency_ms)
        sleep(
            retry_after_seconds
            if retry_after_seconds is not None
            else retry_policy.base_delay_seconds * 2.0**attempt
        )
    raise AssertionError("A valid Jev retry policy always returns or receives a response")


def evaluate_persisted_prompt(
    prompt: PromptVersion,
    values: Mapping[str, str],
    context: ModelCallContext,
    persistence: ModelCallPersistence,
    *,
    api_key: str,
    policy: JevPolicy = "atomic",
    sender: JevSender | None = None,
    retry_policy: JevRetryPolicy | None = None,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CriterionResult:
    if context.input_digest != prompt_input_digest(values):
        raise ValueError("Model-call context does not match the prompt input")
    request_id = model_request_id(context, prompt)
    completed = persistence.find_completed(request_id)
    if isinstance(completed, TerminalOperationalError):
        return completed
    if isinstance(completed, CompletedModelCall):
        return CriterionAccepted.model_validate(completed.parsed_output)
    if isinstance(completed, PendingModelCallUsage):
        raise RuntimeError("Jev model calls cannot have pending usage")

    attempt_number = persistence.next_attempt_number(request_id)

    def record_retry(failure: RetryableOperationalError, latency_ms: int) -> None:
        nonlocal attempt_number
        persistence.record(
            ModelCallAttempt(
                id=uuid4(),
                context=context,
                request_id=request_id,
                attempt_number=attempt_number,
                prompt_name=prompt.definition.name,
                prompt_version_id=prompt.id,
                requested_model=JEV_MODEL,
                response_model=None,
                provider_response_id=None,
                status="retryable_error",
                parsed_output=None,
                raw_response=None,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                latency_ms=latency_ms,
                error={"code": failure.error_code, "message": failure.reason},
                observed_at=now(),
            )
        )
        attempt_number += 1

    result = evaluate_prompt(
        prompt,
        values,
        api_key=api_key,
        policy=policy,
        sender=sender,
        retry_policy=retry_policy,
        sleep=sleep,
        clock=clock,
        observe_retry=record_retry,
    )
    if isinstance(result, JevCriterionObservation):
        accepted = result.result
        attempt = ModelCallAttempt(
            id=uuid4(),
            context=context,
            request_id=request_id,
            attempt_number=attempt_number,
            prompt_name=prompt.definition.name,
            prompt_version_id=prompt.id,
            requested_model=JEV_MODEL,
            response_model=result.model,
            provider_response_id=result.provider_request_id,
            status="accepted",
            parsed_output=accepted.model_dump(mode="json"),
            raw_response=None,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.estimated_cost_usd,
            latency_ms=result.latency_ms,
            error=None,
            observed_at=now(),
        )
        persistence.record(attempt)
        return accepted

    attempt = ModelCallAttempt(
        id=uuid4(),
        context=context,
        request_id=request_id,
        attempt_number=attempt_number,
        prompt_name=prompt.definition.name,
        prompt_version_id=prompt.id,
        requested_model=JEV_MODEL,
        response_model=None,
        provider_response_id=None,
        status="retryable_error"
        if isinstance(result, RetryableOperationalError)
        else "terminal_error",
        parsed_output=None,
        raw_response=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        latency_ms=0,
        error={"code": result.error_code, "message": result.reason},
        observed_at=now(),
    )
    persistence.record(attempt)
    return result


def _compose_atomic(criterion: str, probabilities: Mapping[str, float]) -> tuple[bool, float]:
    active = {
        name for name, probability in probabilities.items() if probability >= JEV_PASS_THRESHOLD
    }
    if criterion in ("remote-europe-eligible", "compensation-minimum", "role-quality"):
        failure_probability = max(probabilities.values())
        return not active, 1.0 - failure_probability
    if criterion == "cheap-shop-placement":
        ordered = sorted(probabilities.values(), reverse=True)
        second_signal = ordered[1] if len(ordered) > 1 else 0.0
        return len(active) < 2, 1.0 - second_signal
    positive, exclusion = {
        "early-stage-product-engineer": ("owns_product_delivery", "excluded_primary_shape"),
        "applied-ai-product-engineer": ("ships_ai_product", "excluded_ai_shape"),
    }[criterion]
    pass_probability = min(probabilities[positive], 1.0 - probabilities[exclusion])
    return positive in active and exclusion not in active, pass_probability


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
