from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

import pytest
import requests
from pydantic import ValidationError

from job_finder.evaluation.jev import (
    JEV_MODEL,
    JEV_QUESTIONS,
    JevCriterionObservation,
    JevHttpResponse,
    JevRunMetrics,
    JevRetryPolicy,
    JevSystemOneResponse,
    evaluate_prompt,
    jev_policy_digest,
    summarize_observations,
)
from job_finder.evaluation.models import (
    CriterionAccepted,
    RetryableOperationalError,
    TerminalOperationalError,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.evaluation.prompts import EVALUATION_PROMPTS


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
        questions = cast(dict[str, dict[str, object]], body["questions"])
        question = questions["compensation-minimum"]
        assert question["type"] == "noul"
        assert "1 EUR ~= 1.10 USD" in cast(str, question["instructions"])
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
    )


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
