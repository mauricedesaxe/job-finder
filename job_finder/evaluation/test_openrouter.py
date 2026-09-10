from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
import requests

from job_finder.evaluation.models import (
    CompletedModelCall,
    CriterionAccepted,
    RetryableOperationalError,
    TerminalOperationalError,
    ModelCallAttempt,
    ModelCallContext,
)
from job_finder.evaluation.openrouter import (
    HttpResponse,
    ModelCallPersistence,
    RetryPolicy,
    evaluate_prompt,
    model_request_id,
    prompt_input_digest,
)
from job_finder.evaluation.prompt_releases import build_prompt_release

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_sends_the_exact_prompt_and_returns_only_after_recording() -> None:
    prompt = build_prompt_release().versions[0]
    events: list[str] = []
    attempts: list[ModelCallAttempt] = []

    def send(
        url: str, headers: Mapping[str, str], body: dict[str, object], timeout: float
    ) -> HttpResponse:
        events.append("sent")
        assert url.endswith("/chat/completions")
        assert headers["authorization"] == "Bearer secret"
        assert timeout == 30.0
        assert body["model"] == "google/gemini-2.5-flash"
        assert body["temperature"] == 0
        assert body["max_tokens"] == 256
        assert body["usage"] == {"include": True}
        messages = cast(list[dict[str, str]], body["messages"])
        assert messages[1] == {"role": "user", "content": "job body"}
        return _accepted_response(pass_value=True, reason="matched")

    def record(attempt: ModelCallAttempt) -> None:
        events.append("recorded")
        attempts.append(attempt)

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=record,
        ),
        api_key="secret",
        sender=send,
        now=lambda: NOW,
    )

    assert events == ["sent", "recorded"]
    assert result == CriterionAccepted(
        prompt_name=prompt.definition.name, passed=True, reason="matched"
    )
    assert attempts[0].status == "accepted"
    assert attempts[0].provider_response_id == "generation-1"
    assert attempts[0].response_model == "google/gemini-2.5-flash-001"
    assert attempts[0].input_tokens == 12
    assert attempts[0].output_tokens == 4
    assert str(attempts[0].cost_usd) == "0.00012"


def test_records_each_retry_before_the_next_request() -> None:
    prompt = build_prompt_release().versions[0]
    events: list[str] = []
    responses = iter(
        (
            HttpResponse(429, '{"error":{"message":"slow down"}}'),
            _accepted_response(pass_value=False, reason="not remote"),
        )
    )

    def send(
        _url: str, _headers: Mapping[str, str], _body: dict[str, object], _timeout: float
    ) -> HttpResponse:
        events.append("sent")
        return next(responses)

    def record(attempt: ModelCallAttempt) -> None:
        events.append(f"recorded:{attempt.attempt_number}:{attempt.status}")

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=record,
        ),
        api_key="secret",
        sender=send,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        sleep=lambda _delay: events.append("slept"),
        now=lambda: NOW,
    )

    assert events == [
        "sent",
        "recorded:0:retryable_error",
        "slept",
        "sent",
        "recorded:1:accepted",
    ]
    assert isinstance(result, CriterionAccepted)
    assert result.passed is False


def test_retries_network_and_malformed_responses_without_defaulting_a_verdict() -> None:
    prompt = build_prompt_release().versions[0]
    attempts: list[ModelCallAttempt] = []
    calls = 0

    def send(
        _url: str, _headers: Mapping[str, str], _body: dict[str, object], _timeout: float
    ) -> HttpResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.Timeout("timed out")
        return HttpResponse(200, '{"id":"bad"}')

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=attempts.append,
        ),
        api_key="secret",
        sender=send,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        sleep=lambda _delay: None,
        now=lambda: NOW,
    )

    assert result == RetryableOperationalError(
        prompt_name=prompt.definition.name,
        error_code="invalid_response",
        reason=result.reason,
    )
    assert [attempt.status for attempt in attempts] == ["retryable_error", "retryable_error"]
    assert all(attempt.parsed_output is None for attempt in attempts)


def test_does_not_retry_terminal_http_errors() -> None:
    prompt = build_prompt_release().versions[0]
    attempts: list[ModelCallAttempt] = []
    calls = 0

    def send(
        _url: str, _headers: Mapping[str, str], _body: dict[str, object], _timeout: float
    ) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse(400, '{"error":{"message":"bad request"}}')

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=attempts.append,
        ),
        api_key="secret",
        sender=send,
        retry_policy=RetryPolicy(max_attempts=4, base_delay_seconds=0),
        now=lambda: NOW,
    )

    assert calls == 1
    assert isinstance(result, TerminalOperationalError)
    assert result.error_code == "http_400"
    assert attempts[0].status == "terminal_error"


def test_returns_a_retryable_error_after_network_attempts_are_exhausted() -> None:
    prompt = build_prompt_release().versions[0]

    def send(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        raise requests.Timeout("timed out")

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=lambda _attempt: None,
        ),
        api_key="secret",
        sender=send,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
        now=lambda: NOW,
    )

    assert isinstance(result, RetryableOperationalError)
    assert result.error_code == "network_error"


@pytest.mark.parametrize("status_code", (429, 500, 502, 503))
def test_returns_retryable_errors_after_retryable_http_attempts_are_exhausted(
    status_code: int,
) -> None:
    prompt = build_prompt_release().versions[0]
    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=lambda _attempt: None,
        ),
        api_key="secret",
        sender=lambda _url, _headers, _body, _timeout: HttpResponse(
            status_code, '{"error":{"message":"try again"}}'
        ),
        retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
        now=lambda: NOW,
    )

    assert isinstance(result, RetryableOperationalError)
    assert result.error_code == f"http_{status_code}"


def test_reuses_a_cached_terminal_error_without_calling_openrouter() -> None:
    prompt = build_prompt_release().versions[0]
    cached = TerminalOperationalError(
        prompt_name=prompt.definition.name,
        error_code="http_400",
        reason="bad request",
    )

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: cached,
            next_attempt_number=lambda _request_id: pytest.fail("attempt number was requested"),
            record=lambda _attempt: pytest.fail("cached error was recorded again"),
        ),
        api_key="secret",
        sender=_unexpected_send,
        now=lambda: NOW,
    )

    assert result == cached


def test_rejects_invalid_retry_policies() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _ = RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="negative"):
        _ = RetryPolicy(base_delay_seconds=-1)


def test_reuses_an_accepted_request_without_calling_openrouter() -> None:
    prompt = build_prompt_release().versions[0]
    accepted = CompletedModelCall(
        prompt_name=prompt.definition.name,
        parsed_output={"pass": True, "reason": "stored"},
    )
    context = _context()

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        context,
        ModelCallPersistence(
            find_completed=lambda _request_id: accepted,
            next_attempt_number=lambda _request_id: 0,
            record=_unexpected_record,
        ),
        api_key="secret",
        sender=_unexpected_send,
    )

    assert result == CriterionAccepted(
        prompt_name=prompt.definition.name, passed=True, reason="stored"
    )
    assert len(model_request_id(context, prompt)) == 64
    assert model_request_id(context, prompt) == model_request_id(context, prompt)
    assert prompt_input_digest({"job": "job body"}) != prompt_input_digest({"job": "changed"})


def test_rejects_a_context_for_different_prompt_input() -> None:
    prompt = build_prompt_release().versions[0]
    context = ModelCallContext(
        processing_attempt_id=UUID("00000000-0000-0000-0000-000000000001"),
        pipeline_run_id=UUID("00000000-0000-0000-0000-000000000002"),
        prompt_release_id=build_prompt_release().id,
        operation_key="evaluate_job",
        input_digest=prompt_input_digest({"job": "different"}),
    )

    with pytest.raises(ValueError, match="does not match"):
        _ = evaluate_prompt(
            prompt,
            {"job": "job body"},
            context,
            ModelCallPersistence(
                find_completed=lambda _request_id: None,
                next_attempt_number=lambda _request_id: 0,
                record=_unexpected_record,
            ),
            api_key="secret",
            sender=_unexpected_send,
        )


def test_does_not_expand_placeholders_inside_job_text() -> None:
    prompt = build_prompt_release().versions[1]
    bodies: list[dict[str, object]] = []
    values = {"job": "Keep the literal {rates} text", "rates": "1 EUR ~= 1.10 USD"}
    context = ModelCallContext(
        processing_attempt_id=UUID("00000000-0000-0000-0000-000000000001"),
        pipeline_run_id=UUID("00000000-0000-0000-0000-000000000002"),
        prompt_release_id=build_prompt_release().id,
        operation_key="evaluate_job",
        input_digest=prompt_input_digest(values),
    )

    def send(
        _url: str, _headers: Mapping[str, str], body: dict[str, object], _timeout: float
    ) -> HttpResponse:
        bodies.append(body)
        return _accepted_response(pass_value=True, reason="matched")

    _ = evaluate_prompt(
        prompt,
        values,
        context,
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=lambda _attempt: None,
        ),
        api_key="secret",
        sender=send,
        now=lambda: NOW,
    )

    messages = cast(list[dict[str, str]], bodies[0]["messages"])
    assert messages[1]["content"] == "Keep the literal {rates} text"


def test_continues_attempt_numbers_after_a_restart() -> None:
    prompt = build_prompt_release().versions[0]
    attempts: list[ModelCallAttempt] = []

    result = evaluate_prompt(
        prompt,
        {"job": "job body"},
        _context(),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 2,
            record=attempts.append,
        ),
        api_key="secret",
        sender=_accepted_sender,
        now=lambda: NOW,
    )

    assert isinstance(result, CriterionAccepted)
    assert attempts[0].attempt_number == 2


def test_does_not_return_an_uncommitted_accepted_result() -> None:
    prompt = build_prompt_release().versions[0]

    def fail_record(_attempt: ModelCallAttempt) -> None:
        raise RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        _ = evaluate_prompt(
            prompt,
            {"job": "job body"},
            _context(),
            ModelCallPersistence(
                find_completed=lambda _request_id: None,
                next_attempt_number=lambda _request_id: 0,
                record=fail_record,
            ),
            api_key="secret",
            sender=_accepted_sender,
            now=lambda: NOW,
        )


def _context() -> ModelCallContext:
    release = build_prompt_release()
    return ModelCallContext(
        processing_attempt_id=UUID("00000000-0000-0000-0000-000000000001"),
        pipeline_run_id=UUID("00000000-0000-0000-0000-000000000002"),
        prompt_release_id=release.id,
        operation_key="evaluate_job",
        input_digest=prompt_input_digest({"job": "job body"}),
    )


def _accepted_response(*, pass_value: bool, reason: str) -> HttpResponse:
    return HttpResponse(
        200,
        json.dumps(
            {
                "id": "generation-1",
                "model": "google/gemini-2.5-flash-001",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "evaluate_job",
                                        "arguments": json.dumps(
                                            {"pass": pass_value, "reason": reason}
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "cost": 0.00012,
                },
            }
        ),
    )


def _accepted_sender(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> HttpResponse:
    return _accepted_response(pass_value=True, reason="matched")


def _unexpected_send(*_args: object) -> HttpResponse:
    raise AssertionError("OpenRouter should not be called")


def _unexpected_record(_attempt: ModelCallAttempt) -> None:
    raise AssertionError("accepted result should not be recorded twice")
