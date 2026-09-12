from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace as dataclass_replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar, Literal, TypeVar
from uuid import uuid4

import psycopg
import requests
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from job_finder.evaluation.models import (
    CompletedModelCall,
    CriterionAccepted,
    CriterionResult,
    EvaluationModel,
    OperationalError,
    OperationalFailure,
    EvaluationToolOutput,
    ModelCallAttempt,
    InputDigest,
    ModelCallContext,
    ModelRequestId,
    PromptAccepted,
    RetryableOperationalError,
    TerminalOperationalError,
)
from job_finder.evaluation.prompt_releases import PromptVersion

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"
RETRYABLE_HTTP_STATUSES = frozenset((429, 500, 502, 503))
RETRYABLE_GENERATION_HTTP_STATUSES = RETRYABLE_HTTP_STATUSES | frozenset((404, 524, 529))
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_INTEGER: TypeAdapter[int] = TypeAdapter(int)
_OutputT = TypeVar("_OutputT", bound=EvaluationModel)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: str


@dataclass(frozen=True)
class PendingModelCallUsage:
    prompt_name: str
    response_model: str
    provider_response_id: str
    raw_response: JsonValue


ChatCompletionSender = Callable[[str, Mapping[str, str], dict[str, object], float], HttpResponse]
GenerationSender = Callable[[str, Mapping[str, str], str, float], HttpResponse]
AttemptRecorder = Callable[[ModelCallAttempt], None]
CompletedLookup = Callable[
    [ModelRequestId], CompletedModelCall | PendingModelCallUsage | TerminalOperationalError | None
]
AttemptNumberLookup = Callable[[ModelRequestId], int]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
Now = Callable[[], datetime]


@dataclass(frozen=True)
class ModelCallPersistence:
    find_completed: CompletedLookup
    next_attempt_number: AttemptNumberLookup
    record: AttemptRecorder


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("Retry policy requires at least one attempt")
        if self.base_delay_seconds < 0:
            raise ValueError("Retry delay cannot be negative")


class OpenRouterModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")


class OpenRouterFunction(OpenRouterModel):
    name: str
    arguments: str


class OpenRouterToolCall(OpenRouterModel):
    type: Literal["function"]
    function: OpenRouterFunction


class OpenRouterMessage(OpenRouterModel):
    tool_calls: tuple[OpenRouterToolCall, ...] = ()


class OpenRouterChoice(OpenRouterModel):
    message: OpenRouterMessage
    finish_reason: str | None = None
    native_finish_reason: str | None = None


class OpenRouterUsage(OpenRouterModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost: Decimal = Field(ge=0)


class OpenRouterCompletion(OpenRouterModel):
    id: str
    model: str
    choices: tuple[OpenRouterChoice, ...] = Field(min_length=1)
    usage: OpenRouterUsage | None = None


class OpenRouterGenerationData(OpenRouterModel):
    tokens_prompt: int = Field(ge=0)
    tokens_completion: int = Field(ge=0)
    total_cost: Decimal = Field(ge=0)


class OpenRouterGeneration(OpenRouterModel):
    data: OpenRouterGenerationData


@dataclass(frozen=True)
class UsageUnavailable:
    reason: str
    retryable: bool


class StoredModelCallError(OpenRouterModel):
    code: str
    message: str


def evaluate_prompt(
    prompt: PromptVersion,
    values: Mapping[str, str],
    context: ModelCallContext,
    persistence: ModelCallPersistence,
    *,
    api_key: str,
    sender: ChatCompletionSender | None = None,
    generation_sender: GenerationSender | None = None,
    retry_policy: RetryPolicy | None = None,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    now: Now = lambda: datetime.now(UTC),
) -> CriterionResult:
    result = invoke_prompt(
        prompt,
        values,
        context,
        persistence,
        EvaluationToolOutput,
        api_key=api_key,
        sender=sender,
        generation_sender=generation_sender,
        retry_policy=retry_policy,
        sleep=sleep,
        clock=clock,
        now=now,
    )
    if isinstance(result, OperationalError):
        return result
    return CriterionAccepted(
        prompt_name=result.prompt_name,
        passed=result.output.passed,
        reason=result.output.reason,
    )


def invoke_prompt(
    prompt: PromptVersion,
    values: Mapping[str, str],
    context: ModelCallContext,
    persistence: ModelCallPersistence,
    output_type: type[_OutputT],
    *,
    api_key: str,
    sender: ChatCompletionSender | None = None,
    generation_sender: GenerationSender | None = None,
    retry_policy: RetryPolicy | None = None,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    now: Now = lambda: datetime.now(UTC),
) -> PromptAccepted[_OutputT] | OperationalFailure:
    policy = retry_policy or RetryPolicy()
    messages = _render_messages(prompt, values)
    if context.input_digest != prompt_input_digest(values):
        raise ValueError("Model-call context does not match the prompt input")
    request_id = model_request_id(context, prompt)
    completed = persistence.find_completed(request_id)
    if isinstance(completed, TerminalOperationalError):
        return completed
    if completed is not None:
        if isinstance(completed, PendingModelCallUsage):
            return _resume_usage_lookup(
                completed,
                prompt,
                context,
                request_id,
                persistence,
                output_type,
                messages,
                api_key,
                generation_sender or send_generation,
                policy,
                sleep,
                clock,
                now,
            )
        return PromptAccepted(
            prompt_name=completed.prompt_name,
            output=output_type.model_validate(completed.parsed_output),
        )
    first_attempt_number = persistence.next_attempt_number(request_id)
    body = _request_body(prompt, messages)
    send = sender or send_chat_completion
    get_generation = generation_sender or send_generation
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    for attempt_number in range(first_attempt_number, first_attempt_number + policy.max_attempts):
        started_at = clock()
        try:
            response = send(
                OPENROUTER_CHAT_COMPLETIONS_URL,
                headers,
                body,
                30.0,
            )
            latency_ms = max(0, round((clock() - started_at) * 1000))
            result, attempt, retry_completion = _interpret_response(
                response,
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                now(),
                output_type,
                messages,
                headers,
                get_generation,
                policy,
                sleep,
            )
        except requests.RequestException as error:
            latency_ms = max(0, round((clock() - started_at) * 1000))
            result = RetryableOperationalError(
                prompt_name=prompt.definition.name,
                error_code="network_error",
                reason=str(error),
            )
            attempt = _failed_attempt(
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                now(),
                "retryable_error",
                result.error_code,
                result.reason,
                request_messages=messages,
            )
            retry_completion = True
        persistence.record(attempt)
        if isinstance(result, PromptAccepted):
            return result
        attempts_used = attempt_number - first_attempt_number + 1
        if (
            attempt.status != "retryable_error"
            or not retry_completion
            or attempts_used >= policy.max_attempts
        ):
            return result
        sleep(policy.base_delay_seconds * 2.0 ** (attempts_used - 1))
    raise AssertionError("A valid retry policy always returns from the attempt loop")


def _resume_usage_lookup(
    pending: PendingModelCallUsage,
    prompt: PromptVersion,
    context: ModelCallContext,
    request_id: ModelRequestId,
    persistence: ModelCallPersistence,
    output_type: type[_OutputT],
    messages: tuple[dict[str, str], ...],
    api_key: str,
    generation_sender: GenerationSender,
    policy: RetryPolicy,
    sleep: Sleeper,
    clock: Clock,
    now: Now,
) -> PromptAccepted[_OutputT] | OperationalFailure:
    started_at = clock()
    result, attempt, _retry_completion = _interpret_response(
        HttpResponse(status_code=200, body=json.dumps(pending.raw_response)),
        prompt,
        context,
        request_id,
        persistence.next_attempt_number(request_id),
        0,
        now(),
        output_type,
        messages,
        {"authorization": f"Bearer {api_key}", "content-type": "application/json"},
        generation_sender,
        policy,
        sleep,
    )
    attempt = dataclass_replace(
        attempt,
        latency_ms=max(0, round((clock() - started_at) * 1000)),
    )
    persistence.record(attempt)
    return result


def send_chat_completion(
    url: str,
    headers: Mapping[str, str],
    body: dict[str, object],
    timeout_seconds: float,
) -> HttpResponse:
    response = requests.post(url, headers=headers, json=body, timeout=timeout_seconds)
    return HttpResponse(status_code=response.status_code, body=response.text)


def send_generation(
    url: str,
    headers: Mapping[str, str],
    generation_id: str,
    timeout_seconds: float,
) -> HttpResponse:
    response = requests.get(
        url, headers=headers, params={"id": generation_id}, timeout=timeout_seconds
    )
    return HttpResponse(status_code=response.status_code, body=response.text)


def postgres_model_call_persistence(
    connection: psycopg.Connection[tuple[object, ...]],
) -> ModelCallPersistence:
    if not connection.autocommit:
        raise ValueError("Model-call persistence requires an autocommit connection")

    def find_completed(
        request_id: ModelRequestId,
    ) -> CompletedModelCall | PendingModelCallUsage | TerminalOperationalError | None:
        row = connection.execute(
            """
            SELECT prompt_name, status, parsed_output, error, response_model,
                   provider_response_id, raw_response
            FROM model_call_attempts
            WHERE request_id = %s
              AND (
                status IN ('accepted', 'terminal_error')
                OR (
                  status = 'retryable_error'
                  AND error ->> 'code' = 'usage_unavailable'
                  AND response_model IS NOT NULL
                  AND provider_response_id IS NOT NULL
                  AND raw_response IS NOT NULL
                )
              )
            ORDER BY (status = 'accepted') DESC, attempt_number DESC
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if str(row[1]) == "accepted":
            parsed_output = TypeAdapter(dict[str, JsonValue]).validate_python(row[2])
            return CompletedModelCall(prompt_name=str(row[0]), parsed_output=parsed_output)
        if str(row[1]) == "retryable_error":
            return PendingModelCallUsage(
                prompt_name=str(row[0]),
                response_model=str(row[4]),
                provider_response_id=str(row[5]),
                raw_response=_JSON.validate_python(row[6]),
            )
        error = StoredModelCallError.model_validate(row[3])
        return TerminalOperationalError(
            prompt_name=str(row[0]), error_code=error.code, reason=error.message
        )

    def next_attempt_number(request_id: ModelRequestId) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(max(attempt_number), -1) + 1
            FROM model_call_attempts
            WHERE request_id = %s
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Could not read model-call attempt history")
        return _INTEGER.validate_python(row[0])

    def record(attempt: ModelCallAttempt) -> None:
        with connection.transaction():
            _ = connection.execute(
                """
                INSERT INTO model_call_attempts (
                  id, processing_attempt_id, pipeline_run_id, prompt_release_id,
                  request_id, attempt_number, operation_key, prompt_name, request_messages,
                  prompt_version_id, input_digest, requested_model, response_model,
                  provider, provider_response_id, status, parsed_output, raw_response,
                  input_tokens, output_tokens, cost_usd, latency_ms, error, observed_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  'openrouter', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    attempt.id,
                    attempt.context.processing_attempt_id,
                    attempt.context.pipeline_run_id,
                    attempt.context.prompt_release_id,
                    attempt.request_id,
                    attempt.attempt_number,
                    attempt.context.operation_key,
                    attempt.prompt_name,
                    Jsonb(attempt.request_messages),
                    attempt.prompt_version_id,
                    attempt.context.input_digest,
                    attempt.requested_model,
                    attempt.response_model,
                    attempt.provider_response_id,
                    attempt.status,
                    Jsonb(attempt.parsed_output) if attempt.parsed_output is not None else None,
                    Jsonb(attempt.raw_response) if attempt.raw_response is not None else None,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.cost_usd,
                    attempt.latency_ms,
                    Jsonb(attempt.error) if attempt.error is not None else None,
                    attempt.observed_at,
                ),
            )
            enqueue_model_call_projection(connection, attempt)

    return ModelCallPersistence(
        find_completed=find_completed,
        next_attempt_number=next_attempt_number,
        record=record,
    )


def enqueue_model_call_projection(
    connection: psycopg.Connection[tuple[object, ...]], attempt: ModelCallAttempt
) -> None:
    payload = TypeAdapter(dict[str, JsonValue]).validate_json(
        TypeAdapter(ModelCallAttempt).dump_json(attempt)
    )
    payload_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    projection_id = hashlib.sha256(f"model_call:{attempt.id}".encode()).hexdigest()
    _ = connection.execute(
        """
        INSERT INTO langfuse_projection_items (
          id, kind, source_id, payload_digest, payload, state, created_at
        ) VALUES (%s, 'model_call', %s, %s, %s, 'pending', %s)
        ON CONFLICT (kind, source_id) DO NOTHING
        """,
        (
            projection_id,
            str(attempt.id),
            payload_digest,
            Jsonb(payload),
            attempt.observed_at,
        ),
    )


def prompt_input_digest(values: Mapping[str, str]) -> InputDigest:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return InputDigest(hashlib.sha256(encoded).hexdigest())


def model_request_id(context: ModelCallContext, prompt: PromptVersion) -> ModelRequestId:
    identity = [
        str(context.pipeline_run_id),
        str(context.processing_attempt_id),
        context.operation_key,
        str(context.prompt_release_id),
        str(prompt.id),
        str(context.input_digest),
    ]
    encoded = json.dumps(identity, separators=(",", ":")).encode()
    return ModelRequestId(hashlib.sha256(encoded).hexdigest())


def _render_messages(
    prompt: PromptVersion, values: Mapping[str, str]
) -> tuple[dict[str, str], ...]:
    expected = set(prompt.definition.inputs)
    if set(values) != expected:
        raise ValueError(f"Prompt {prompt.definition.name} requires inputs {sorted(expected)}")
    return tuple(
        {
            "role": message["role"],
            "content": _render_template(message["content"], values),
        }
        for message in prompt.messages
    )


def _render_template(template: str, values: Mapping[str, str]) -> str:
    return template.format_map(values)


def _request_body(prompt: PromptVersion, messages: tuple[dict[str, str], ...]) -> dict[str, object]:
    return {
        "model": prompt.model,
        "temperature": prompt.parameters["temperature"],
        "max_tokens": prompt.parameters["max_tokens"],
        "messages": list(messages),
        "usage": {"include": True},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": prompt.tool_name,
                    "description": prompt.tool_description,
                    "parameters": prompt.output_schema,
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": prompt.tool_name}},
    }


def _interpret_response(
    response: HttpResponse,
    prompt: PromptVersion,
    context: ModelCallContext,
    request_id: ModelRequestId,
    attempt_number: int,
    latency_ms: int,
    observed_at: datetime,
    output_type: type[_OutputT],
    request_messages: tuple[dict[str, str], ...],
    headers: Mapping[str, str],
    generation_sender: GenerationSender,
    retry_policy: RetryPolicy,
    sleep: Sleeper,
) -> tuple[PromptAccepted[_OutputT] | OperationalFailure, ModelCallAttempt, bool]:
    try:
        raw = _JSON.validate_json(response.body)
    except ValidationError:
        fallback: dict[str, JsonValue] = {"body": response.body}
        raw = fallback
    if response.status_code != 200:
        status: Literal["retryable_error", "terminal_error"] = (
            "retryable_error"
            if response.status_code in RETRYABLE_HTTP_STATUSES
            else "terminal_error"
        )
        error_type = (
            RetryableOperationalError if status == "retryable_error" else TerminalOperationalError
        )
        unavailable = error_type(
            prompt_name=prompt.definition.name,
            error_code=f"http_{response.status_code}",
            reason=_error_reason(raw, response.status_code),
        )
        return (
            unavailable,
            _failed_attempt(
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                observed_at,
                status,
                unavailable.error_code,
                unavailable.reason,
                raw,
                request_messages=request_messages,
            ),
            status == "retryable_error",
        )
    try:
        completion = OpenRouterCompletion.model_validate(raw)
    except ValidationError as error:
        no_choices = all(item["loc"] == ("choices",) for item in error.errors())
        unavailable = RetryableOperationalError(
            prompt_name=prompt.definition.name,
            error_code="malformed_function_call" if no_choices else "invalid_response",
            reason="response had no usable choices" if no_choices else str(error),
        )
        return (
            unavailable,
            _failed_attempt(
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                observed_at,
                "retryable_error",
                unavailable.error_code,
                unavailable.reason,
                raw,
                request_messages=request_messages,
            ),
            True,
        )
    choice = completion.choices[0]
    if not choice.message.tool_calls:
        finish_reason = choice.native_finish_reason or choice.finish_reason
        reason = (
            f"provider finished with {finish_reason} and returned no tool call"
            if finish_reason is not None
            else "provider returned no tool call"
        )
        unavailable = RetryableOperationalError(
            prompt_name=prompt.definition.name,
            error_code="malformed_function_call",
            reason=reason,
        )
        return (
            unavailable,
            _failed_attempt(
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                observed_at,
                "retryable_error",
                unavailable.error_code,
                unavailable.reason,
                raw,
                request_messages=request_messages,
            ),
            True,
        )
    try:
        tool_call = choice.message.tool_calls[0]
        if tool_call.function.name != prompt.tool_name:
            raise ValueError("response returned the wrong tool")
        output = output_type.model_validate_json(tool_call.function.arguments)
    except (ValidationError, ValueError) as error:
        unavailable = RetryableOperationalError(
            prompt_name=prompt.definition.name,
            error_code="invalid_response",
            reason=str(error),
        )
        return (
            unavailable,
            _failed_attempt(
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                observed_at,
                "retryable_error",
                unavailable.error_code,
                unavailable.reason,
                raw,
                request_messages=request_messages,
            ),
            True,
        )
    usage = completion.usage or _load_generation_usage(
        completion.id,
        headers,
        generation_sender,
        retry_policy,
        sleep,
    )
    if isinstance(usage, UsageUnavailable):
        error_type = RetryableOperationalError if usage.retryable else TerminalOperationalError
        usage_status: Literal["retryable_error", "terminal_error"] = (
            "retryable_error" if usage.retryable else "terminal_error"
        )
        unavailable = error_type(
            prompt_name=prompt.definition.name,
            error_code="usage_unavailable",
            reason=usage.reason,
        )
        return (
            unavailable,
            _failed_attempt(
                prompt,
                context,
                request_id,
                attempt_number,
                latency_ms,
                observed_at,
                usage_status,
                unavailable.error_code,
                unavailable.reason,
                raw,
                request_messages=request_messages,
                response_model=completion.model,
                provider_response_id=completion.id,
            ),
            False,
        )
    parsed_output = output.model_dump(by_alias=True, exclude_none=True)
    accepted = PromptAccepted(prompt_name=prompt.definition.name, output=output)
    return (
        accepted,
        ModelCallAttempt(
            id=uuid4(),
            context=context,
            request_id=request_id,
            attempt_number=attempt_number,
            prompt_name=prompt.definition.name,
            prompt_version_id=prompt.id,
            request_messages=request_messages,
            requested_model=prompt.model,
            response_model=completion.model,
            provider_response_id=completion.id,
            status="accepted",
            parsed_output=parsed_output,
            raw_response=raw,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost_usd=usage.cost,
            latency_ms=latency_ms,
            error=None,
            observed_at=observed_at,
        ),
        False,
    )


def _load_generation_usage(
    generation_id: str,
    headers: Mapping[str, str],
    sender: GenerationSender,
    policy: RetryPolicy,
    sleep: Sleeper,
) -> OpenRouterUsage | UsageUnavailable:
    reason = "OpenRouter generation metadata was unavailable"
    for attempt in range(policy.max_attempts):
        retryable = True
        try:
            response = sender(OPENROUTER_GENERATION_URL, headers, generation_id, 30.0)
            raw = _response_json(response.body)
            if response.status_code == 200:
                metadata = OpenRouterGeneration.model_validate(raw).data
                return OpenRouterUsage(
                    prompt_tokens=metadata.tokens_prompt,
                    completion_tokens=metadata.tokens_completion,
                    cost=metadata.total_cost,
                )
            reason = _error_reason(raw, response.status_code)
            retryable = response.status_code in RETRYABLE_GENERATION_HTTP_STATUSES
        except requests.RequestException as error:
            reason = str(error)
        except ValidationError as error:
            reason = str(error)
        if not retryable or attempt + 1 == policy.max_attempts:
            return UsageUnavailable(reason=reason, retryable=retryable)
        sleep(policy.base_delay_seconds * 2.0**attempt)
    raise AssertionError("A valid retry policy always returns from the metadata loop")


def _response_json(body: str) -> JsonValue:
    try:
        return _JSON.validate_json(body)
    except ValidationError:
        fallback: dict[str, JsonValue] = {"body": body}
        return fallback


def _failed_attempt(
    prompt: PromptVersion,
    context: ModelCallContext,
    request_id: ModelRequestId,
    attempt_number: int,
    latency_ms: int,
    observed_at: datetime,
    status: Literal["retryable_error", "terminal_error"],
    error_code: str,
    reason: str,
    raw_response: JsonValue | None = None,
    *,
    request_messages: tuple[dict[str, str], ...],
    response_model: str | None = None,
    provider_response_id: str | None = None,
) -> ModelCallAttempt:
    return ModelCallAttempt(
        id=uuid4(),
        context=context,
        request_id=request_id,
        attempt_number=attempt_number,
        prompt_name=prompt.definition.name,
        prompt_version_id=prompt.id,
        request_messages=request_messages,
        requested_model=prompt.model,
        response_model=response_model,
        provider_response_id=provider_response_id,
        status=status,
        parsed_output=None,
        raw_response=raw_response,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        latency_ms=latency_ms,
        error={"code": error_code, "message": reason},
        observed_at=observed_at,
    )


def _error_reason(raw: JsonValue, status_code: int) -> str:
    if isinstance(raw, dict):
        error = raw.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
    return f"OpenRouter returned HTTP {status_code}"
