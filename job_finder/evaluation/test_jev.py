from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
import requests
from pydantic import ValidationError

from job_finder.evaluation.jev import (
    ATOMIC_QUESTIONS,
    JEV_MODEL,
    JEV_QUESTIONS,
    JevCriterionObservation,
    JevHttpResponse,
    JevRunMetrics,
    JevRetryPolicy,
    JevSystemOneRequest,
    JevSystemOneResponse,
    evaluate_prompt,
    evaluate_persisted_prompt,
    jev_policy_digest,
    summarize_observations,
)
from job_finder.evaluation.models import (
    CompletedModelCall,
    CriterionAccepted,
    ModelCallAttempt,
    ModelCallContext,
    RetryableOperationalError,
    TerminalOperationalError,
)
from job_finder.evaluation.openrouter import ModelCallPersistence, prompt_input_digest
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.evaluation.prompts import EVALUATION_PROMPTS
from job_finder.evaluation.relevance_releases import (
    RelevanceQuestion,
    ThresholdComposition,
    build_jev_faithful_policy,
)


def test_validates_the_frozen_noul_response_boundary() -> None:
    response = JevSystemOneResponse.model_validate_json(_response_body(0.75))

    assert response.model == JEV_MODEL
    assert response.answers["remote-europe-eligible"].noul == 0.75
    with pytest.raises(ValidationError):
        setattr(response, "model", "jev-latest")
    with pytest.raises(ValidationError):
        _ = JevSystemOneResponse.model_validate_json(
            _response_body(0.75).replace(JEV_MODEL, "jev-latest")
        )
    with pytest.raises(ValidationError):
        _ = JevSystemOneResponse.model_validate_json(_response_body(1.01))
    with pytest.raises(ValidationError):
        _ = JevSystemOneResponse.model_validate_json(
            json.dumps(
                {
                    "model": JEV_MODEL,
                    "answers": {"remote-europe-eligible": {"type": "noul", "noul": 0.75}},
                    "usage": {"input_tokens": 100, "output_tokens": 4},
                    "unexpected": True,
                }
            )
        )


def test_registry_covers_exactly_the_six_evaluation_criteria_and_is_immutable() -> None:
    expected = {prompt.criterion for prompt in EVALUATION_PROMPTS}

    assert len(expected) == 6
    assert set(JEV_QUESTIONS) == expected
    with pytest.raises(TypeError):
        cast(dict[str, object], JEV_QUESTIONS)["other"] = object()


def test_atomic_registry_covers_the_same_criteria_with_independent_questions() -> None:
    assert set(ATOMIC_QUESTIONS) == set(JEV_QUESTIONS)
    assert len(ATOMIC_QUESTIONS["role-quality"]) == 9
    mobile_question = ATOMIC_QUESTIONS["role-quality"]["mobile_specialist"]
    assert "primarily" in mobile_question.instructions
    assert "peripheral" in mobile_question.instructions
    assert len(ATOMIC_QUESTIONS["cheap-shop-placement"]) == 8


def test_sends_the_pinned_model_and_maps_probability_to_a_deterministic_result() -> None:
    prompt = build_prompt_release().versions[1]
    clock_values = iter((4.0, 4.125))

    def send(
        url: str, headers: Mapping[str, str], body: dict[str, object], timeout: float
    ) -> JevHttpResponse:
        assert url == "https://api.typesafe.ai/v1/systemone"
        assert headers["authorization"] == "Bearer secret"
        assert timeout == 30.0
        assert body["model"] == "jev-1.13.0"
        assert body["state"] == "Salary is EUR 150,000."
        request = JevSystemOneRequest.model_validate(body)
        question = request.questions["compensation-minimum"]
        assert question.type == "noul"
        assert "1 EUR ~= 1.10 USD" in question.instructions
        return JevHttpResponse(status_code=200, body=_response_body(0.5, "compensation-minimum"))

    result = evaluate_prompt(
        prompt,
        {"job": "Salary is EUR 150,000.", "rates": "1 EUR ~= 1.10 USD"},
        api_key="secret",
        sender=send,
        clock=lambda: next(clock_values),
    )

    assert result == JevCriterionObservation(
        result=CriterionAccepted(
            prompt_name=prompt.definition.name,
            passed=True,
            reason="Jev pass probability 0.500 met threshold 0.500.",
        ),
        pass_probability=0.5,
        model=JEV_MODEL,
        input_tokens=100,
        output_tokens=4,
        latency_ms=125,
        estimated_cost_usd=Decimal("0.0000042"),
        raw_response=JevSystemOneResponse.model_validate_json(
            _response_body(0.5, "compensation-minimum")
        ).model_dump(mode="json"),
    )


def test_executes_the_questions_and_threshold_from_a_relevance_release() -> None:
    release = build_prompt_release()
    prompt = release.versions[1]
    policy = build_jev_faithful_policy(release)
    questions = dict(policy.questions)
    questions[prompt.definition.criterion] = RelevanceQuestion(
        instructions="Stored release question using {rates}.",
        true="Stored pass.",
        false="Stored fail.",
    )
    policy = policy.model_copy(
        update={
            "questions": questions,
            "composition": ThresholdComposition(pass_threshold=0.75),
        }
    )

    def send(
        _url: str, _headers: Mapping[str, str], body: dict[str, object], _timeout: float
    ) -> JevHttpResponse:
        request = JevSystemOneRequest.model_validate(body)
        assert request.questions[prompt.definition.criterion].instructions == (
            "Stored release question using 1 EUR ~= 1.10 USD."
        )
        return JevHttpResponse(
            status_code=200,
            body=_response_body(0.6, prompt.definition.criterion),
        )

    result = evaluate_prompt(
        prompt,
        {"job": "Salary is EUR 150,000.", "rates": "1 EUR ~= 1.10 USD"},
        api_key="secret",
        sender=send,
        execution_policy=policy,
        clock=iter((0.0, 0.1)).__next__,
    )

    assert isinstance(result, JevCriterionObservation)
    assert not result.result.passed
    assert result.result.reason == "Jev pass probability 0.600 was below threshold 0.750."


def test_atomic_policy_counts_staffing_signals_in_code() -> None:
    prompt = next(
        version
        for version in build_prompt_release().versions
        if version.definition.criterion == "cheap-shop-placement"
    )

    def send(
        _url: str, _headers: Mapping[str, str], body: dict[str, object], _timeout: float
    ) -> JevHttpResponse:
        questions = JevSystemOneRequest.model_validate(body).questions
        assert set(questions) == set(ATOMIC_QUESTIONS["cheap-shop-placement"])
        probabilities = dict.fromkeys(questions, 0.1)
        probabilities["recruiter_for_client"] = 0.9
        probabilities["placement_business"] = 0.8
        return JevHttpResponse(
            status_code=200,
            body=_multi_response_body(probabilities),
        )

    result = evaluate_prompt(
        prompt,
        {"job": "Our staffing service is hiring for a client."},
        api_key="secret",
        sender=send,
        policy="atomic",
        clock=iter((0.0, 0.1)).__next__,
    )

    assert isinstance(result, JevCriterionObservation)
    assert not result.result.passed
    assert abs(result.pass_probability - 0.2) < 1e-9
    assert "recruiter_for_client=0.900" in result.result.reason


def test_atomic_policy_rejects_mobile_specialists() -> None:
    prompt = next(
        version
        for version in build_prompt_release().versions
        if version.definition.criterion == "role-quality"
    )

    def send(
        _url: str, _headers: Mapping[str, str], body: dict[str, object], _timeout: float
    ) -> JevHttpResponse:
        questions = JevSystemOneRequest.model_validate(body).questions
        probabilities = dict.fromkeys(questions, 0.1)
        probabilities["mobile_specialist"] = 0.9
        return JevHttpResponse(
            status_code=200,
            body=_multi_response_body(probabilities),
        )

    result = evaluate_prompt(
        prompt,
        {"job": "Senior Flutter engineer building native mobile applications."},
        api_key="secret",
        sender=send,
        policy="atomic",
        clock=iter((0.0, 0.1)).__next__,
    )

    assert isinstance(result, JevCriterionObservation)
    assert not result.result.passed
    assert abs(result.pass_probability - 0.1) < 1e-9
    assert "mobile_specialist=0.900" in result.result.reason


@pytest.mark.parametrize("status", (408, 429, 500, 529, 599))
def test_maps_retryable_http_errors(status: int) -> None:
    prompt = build_prompt_release().versions[0]

    result = evaluate_prompt(
        prompt,
        {"job": "Remote in Europe"},
        api_key="secret",
        retry_policy=JevRetryPolicy(max_attempts=1),
        sender=lambda _url, _headers, _body, _timeout: JevHttpResponse(
            status_code=status, body="{}"
        ),
    )

    assert result == RetryableOperationalError(
        prompt_name=prompt.definition.name,
        error_code=f"http_{status}",
        reason=f"Jev returned HTTP {status}",
    )


def test_retries_transient_responses_with_exponential_backoff() -> None:
    prompt = build_prompt_release().versions[0]
    responses = iter(
        (
            JevHttpResponse(status_code=429, body="{}"),
            JevHttpResponse(status_code=529, body="{}", retry_after_seconds=0.75),
            JevHttpResponse(status_code=200, body=_response_body(0.75)),
        )
    )
    delays: list[float] = []
    latencies: list[int] = []

    result = evaluate_prompt(
        prompt,
        {"job": "Remote in Europe"},
        api_key="secret",
        sender=lambda _url, _headers, _body, _timeout: next(responses),
        retry_policy=JevRetryPolicy(max_attempts=3, base_delay_seconds=0.25),
        sleep=delays.append,
        clock=iter((0.0, 0.1, 1.0, 1.2, 2.0, 2.3)).__next__,
        observe_request=latencies.append,
    )

    assert isinstance(result, JevCriterionObservation)
    assert delays == [0.25, 0.75]
    assert latencies == [100, 200, 300]


def test_maps_terminal_network_and_invalid_response_errors() -> None:
    prompt = build_prompt_release().versions[0]

    terminal = evaluate_prompt(
        prompt,
        {"job": "Remote in Europe"},
        api_key="secret",
        sender=lambda _url, _headers, _body, _timeout: JevHttpResponse(status_code=401, body="{}"),
    )

    def timeout(
        _url: str, _headers: Mapping[str, str], _body: dict[str, object], _timeout: float
    ) -> JevHttpResponse:
        raise requests.Timeout("timed out")

    network = evaluate_prompt(
        prompt,
        {"job": "Remote in Europe"},
        api_key="secret",
        sender=timeout,
        retry_policy=JevRetryPolicy(max_attempts=1),
    )
    invalid = evaluate_prompt(
        prompt,
        {"job": "Remote in Europe"},
        api_key="secret",
        sender=lambda _url, _headers, _body, _timeout: JevHttpResponse(
            status_code=200, body="not-json"
        ),
    )

    assert isinstance(terminal, TerminalOperationalError)
    assert terminal.error_code == "http_401"
    assert isinstance(network, RetryableOperationalError)
    assert network.error_code == "network_error"
    assert isinstance(invalid, TerminalOperationalError)
    assert invalid.error_code == "invalid_response"


def test_persists_each_retry_and_the_accepted_jev_result() -> None:
    prompt = build_prompt_release().versions[0]
    values = {"job": "Remote in Europe"}
    attempts: list[ModelCallAttempt] = []
    responses = iter((JevHttpResponse(status_code=503, body="{}"),))
    observed_at = datetime(2026, 9, 20, tzinfo=UTC)

    def send(
        _url: str,
        _headers: Mapping[str, str],
        body: dict[str, object],
        _timeout: float,
    ) -> JevHttpResponse:
        response = next(responses, None)
        if response is not None:
            return response
        questions = JevSystemOneRequest.model_validate(body).questions
        return JevHttpResponse(
            status_code=200,
            body=_multi_response_body(dict.fromkeys(questions, 0.75)),
            provider_request_id="typesafe-request-1",
        )

    result = evaluate_persisted_prompt(
        prompt,
        values,
        _context(values),
        ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 3,
            record=attempts.append,
        ),
        api_key="secret",
        sender=send,
        retry_policy=JevRetryPolicy(max_attempts=2, base_delay_seconds=0),
        sleep=lambda _delay: None,
        clock=iter((0.0, 0.1, 1.0, 1.2)).__next__,
        now=lambda: observed_at,
    )

    assert isinstance(result, CriterionAccepted)
    assert [attempt.attempt_number for attempt in attempts] == [3, 4]
    assert [attempt.status for attempt in attempts] == ["retryable_error", "accepted"]
    assert attempts[0].error == {"code": "http_503", "message": "Jev returned HTTP 503"}
    assert attempts[0].latency_ms == 100
    assert attempts[1].requested_model == JEV_MODEL
    assert attempts[1].response_model == JEV_MODEL
    assert attempts[1].provider_response_id == "typesafe-request-1"
    assert attempts[1].input_tokens == 100
    assert attempts[1].output_tokens == 4
    assert attempts[1].observed_at == observed_at


def test_reuses_a_persisted_jev_result_without_calling_the_provider() -> None:
    prompt = build_prompt_release().versions[0]
    values = {"job": "Remote in Europe"}

    result = evaluate_persisted_prompt(
        prompt,
        values,
        _context(values),
        ModelCallPersistence(
            find_completed=lambda _request_id: CompletedModelCall(
                prompt_name=prompt.definition.name,
                parsed_output={
                    "prompt_name": prompt.definition.name,
                    "passed": True,
                    "reason": "stored",
                },
            ),
            next_attempt_number=lambda _request_id: pytest.fail("attempt number was read"),
            record=lambda _attempt: pytest.fail("cached result was recorded again"),
        ),
        api_key="secret",
        sender=_unexpected_send,
    )

    assert result == CriterionAccepted(
        prompt_name=prompt.definition.name, passed=True, reason="stored"
    )


def test_summarizes_tokens_cost_and_interpolated_latency_percentiles() -> None:
    observations = (
        _observation(input_tokens=100, output_tokens=4, latency_ms=100),
        _observation(input_tokens=300, output_tokens=8, latency_ms=300),
    )

    metrics = summarize_observations(observations)

    assert metrics.request_count == 2
    assert metrics.input_tokens == 400
    assert metrics.output_tokens == 12
    assert metrics.estimated_cost_usd == Decimal("0.0000168")
    assert metrics.p50_latency_ms == 200
    assert metrics.p95_latency_ms == 290

    metrics_with_failed_attempt = summarize_observations(observations, (50, 100, 300))
    assert metrics_with_failed_attempt.request_count == 3
    assert metrics_with_failed_attempt.p50_latency_ms == 100


def test_jev_policy_identity_is_stable() -> None:
    digest = jev_policy_digest("1 EUR ~= 1.10 USD")
    assert digest == "21b93830db6d2d425e16d4ed521615fc96bffc17f862034ac901cde3eb09251b"
    assert digest != jev_policy_digest("1 EUR ~= 1.20 USD")


@pytest.mark.parametrize(
    ("p50", "p95"),
    ((None, 1.0), (1.0, None)),
)
def test_rejects_partial_latency_metrics(p50: float | None, p95: float | None) -> None:
    with pytest.raises(ValidationError, match="both be present or absent"):
        JevRunMetrics(
            request_count=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost_usd=Decimal(0),
            p50_latency_ms=p50,
            p95_latency_ms=p95,
        )


def _response_body(probability: float, criterion: str = "remote-europe-eligible") -> str:
    return json.dumps(
        {
            "model": JEV_MODEL,
            "answers": {criterion: {"type": "noul", "noul": probability}},
            "usage": {"input_tokens": 100, "output_tokens": 4},
        }
    )


def _multi_response_body(probabilities: Mapping[str, float]) -> str:
    return json.dumps(
        {
            "model": JEV_MODEL,
            "answers": {
                name: {"type": "noul", "noul": probability}
                for name, probability in probabilities.items()
            },
            "usage": {"input_tokens": 100, "output_tokens": len(probabilities)},
        }
    )


def _observation(
    *, input_tokens: int, output_tokens: int, latency_ms: int
) -> JevCriterionObservation:
    return JevCriterionObservation(
        result=CriterionAccepted(prompt_name="prompt", passed=True, reason="reason"),
        pass_probability=0.9,
        model=JEV_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        estimated_cost_usd=(Decimal(input_tokens) * Decimal("0.042") / Decimal(1_000_000)),
    )


def _context(values: Mapping[str, str]) -> ModelCallContext:
    release = build_prompt_release()
    return ModelCallContext(
        processing_attempt_id=UUID("00000000-0000-0000-0000-000000000001"),
        pipeline_run_id=UUID("00000000-0000-0000-0000-000000000002"),
        prompt_release_id=release.id,
        operation_key="evaluate_job",
        input_digest=prompt_input_digest(values),
    )


def _unexpected_send(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> JevHttpResponse:
    pytest.fail("cached result reached Jev")
