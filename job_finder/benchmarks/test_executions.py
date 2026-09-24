from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from job_finder.benchmarks.executions import (
    EvaluateManifestCommand,
    EvaluationRun,
    EvaluationRunTelemetry,
    RunningEvaluationExecution,
    aggregate_evaluation_telemetry,
    exchange_rate_snapshot_digest,
)
from job_finder.benchmarks.scoring import EvaluationMetrics, EvaluationTrialResult
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import (
    ProviderRequestObservation,
    PromptReleaseId,
    ReleaseTarget,
    RelevanceReleaseId,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_evaluation_run_exposes_only_complete_release_targets() -> None:
    prompt_release_id = PromptReleaseId("b" * 64)
    target = ReleaseTarget(
        prompt_release_id=prompt_release_id,
        relevance_release_id=RelevanceReleaseId("c" * 64),
    )
    run = EvaluationRun(
        id="d" * 64,
        idempotency_key="run",
        manifest_id="a" * 64,
        prompt_release_id=prompt_release_id,
        target=target,
        implementation_ref="test",
        metrics=EvaluationMetrics(
            result_count=1,
            false_positive_count=0,
            false_negative_count=0,
            operational_failure_count=0,
            critical_false_positive_count=0,
            false_positive_rate=Decimal(0),
            false_negative_rate=Decimal(0),
        ),
        results=(
            EvaluationTrialResult(
                id="1" * 64,
                case_position=0,
                trial_index=0,
                expected_outcome="qualified",
                actual_outcome="qualified",
                failure_kind=None,
                reason="Fixture result.",
            ),
        ),
        completed_at=NOW,
    )

    with pytest.raises(ValidationError, match="match prompt provenance"):
        EvaluationRun.model_validate(
            {
                **run.model_dump(mode="json"),
                "target": {
                    "prompt_release_id": "e" * 64,
                    "relevance_release_id": "c" * 64,
                },
            }
        )


def test_exchange_rate_digest_is_canonical_and_execution_rejects_a_mismatch() -> None:
    first = ExchangeRateSnapshot(
        rates={"GBP": Decimal("1.27"), "EUR": Decimal("1.10")},
        source="fallback",
        observed_at=NOW,
    )
    reordered = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.10"), "GBP": Decimal("1.27")},
        source="fallback",
        observed_at=NOW,
    )
    digest = exchange_rate_snapshot_digest(first)

    assert digest == exchange_rate_snapshot_digest(reordered)
    with pytest.raises(ValidationError, match="does not match"):
        RunningEvaluationExecution(
            id="f" * 64,
            command=EvaluateManifestCommand(
                idempotency_key="evaluation:test",
                manifest_id="a" * 64,
                target=ReleaseTarget(
                    prompt_release_id=PromptReleaseId("b" * 64),
                    relevance_release_id=RelevanceReleaseId("c" * 64),
                ),
                implementation_ref="test",
            ),
            exchange_rates=first,
            exchange_rate_digest="0" * 64,
            created_at=NOW,
        )


def test_aggregate_evaluation_telemetry_includes_every_observed_request() -> None:
    telemetry = aggregate_evaluation_telemetry(
        (
            ProviderRequestObservation(
                input_tokens=10,
                output_tokens=2,
                cost_usd=Decimal("0.01"),
                latency_ms=10,
            ),
            ProviderRequestObservation(latency_ms=20),
            ProviderRequestObservation(
                input_tokens=5,
                output_tokens=1,
                cost_usd=Decimal("0.02"),
                latency_ms=100,
            ),
        )
    )

    assert telemetry == EvaluationRunTelemetry(
        request_count=3,
        input_tokens=15,
        output_tokens=3,
        cost_usd=Decimal("0.03"),
        p50_latency_ms=Decimal("20"),
        p95_latency_ms=Decimal("92.0"),
    )
    assert aggregate_evaluation_telemetry(()).p50_latency_ms is None


def test_telemetry_requires_both_percentiles_exactly_when_requests_exist() -> None:
    with pytest.raises(ValidationError, match="both be present"):
        EvaluationRunTelemetry(
            request_count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal(0),
            p50_latency_ms=Decimal(1),
        )
    with pytest.raises(ValidationError, match="at least one request"):
        EvaluationRunTelemetry(
            request_count=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal(0),
            p50_latency_ms=Decimal(1),
            p95_latency_ms=Decimal(1),
        )
    with pytest.raises(ValidationError, match="p50 latency cannot exceed p95 latency"):
        EvaluationRunTelemetry(
            request_count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal(0),
            p50_latency_ms=Decimal(2),
            p95_latency_ms=Decimal(1),
        )


def test_aggregate_evaluation_telemetry_marks_incomplete_usage() -> None:
    telemetry = aggregate_evaluation_telemetry(
        (ProviderRequestObservation(latency_ms=10, usage_complete=False),)
    )

    assert telemetry.request_count == 1
    assert not telemetry.usage_complete

    recovered = aggregate_evaluation_telemetry(
        (
            ProviderRequestObservation(latency_ms=10, usage_complete=False),
            ProviderRequestObservation(
                input_tokens=5,
                output_tokens=2,
                cost_usd=Decimal("0.01"),
                resolves_prior_usage=True,
                latency_ms=5,
            ),
        )
    )
    assert recovered.usage_complete
