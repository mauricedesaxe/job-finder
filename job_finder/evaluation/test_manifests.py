from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.manifests import (
    EvaluationCaseInput,
    EvaluationManifest,
    EvaluationManifestCase,
    EvaluationMetrics,
    EvaluationRun,
    EvaluationRunTelemetry,
    EvaluationTrialResult,
    ManifestPolicy,
    aggregate_evaluation_telemetry,
    compare_runs,
    exchange_rate_snapshot_digest,
    score_results,
)
from job_finder.evaluation.models import (
    ProviderRequestObservation,
    PromptReleaseId,
    ReleaseTarget,
    RelevanceReleaseId,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_scores_false_positives_false_negatives_and_operational_failures_separately() -> None:
    manifest = _manifest()
    results = (
        _result(0, 0, "rejected", "qualified", "false_positive"),
        _result(0, 1, "rejected", "rejected", None),
        _result(0, 2, "rejected", "rejected", None),
        _result(1, 0, "qualified", "rejected", "false_negative"),
        _result(2, 0, "qualified", None, "operational"),
    )

    metrics = score_results(manifest, results)

    assert metrics.result_count == 5
    assert metrics.false_positive_count == 1
    assert metrics.false_positive_rate == Decimal("0.3333333")
    assert metrics.false_negative_count == 1
    assert metrics.false_negative_rate == Decimal("0.5000000")
    assert metrics.operational_failure_count == 1
    assert metrics.critical_false_positive_count == 1


def test_rejects_results_that_do_not_cover_each_trial_once() -> None:
    manifest = _manifest()
    incomplete = (_result(0, 0, "rejected", "rejected", None),)

    with pytest.raises(ValueError, match="configured trial"):
        score_results(manifest, incomplete)


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
        results=(_result(0, 0, "qualified", "qualified", None),),
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


def test_exchange_rate_digest_is_canonical_across_rate_ordering() -> None:
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


def test_compares_case_transitions_deterministically_for_distinct_release_targets() -> None:
    manifest = _manifest()
    baseline_results = (
        _result(0, 0, "rejected", "qualified", "false_positive"),
        _result(0, 1, "rejected", "rejected", None),
        _result(0, 2, "rejected", "rejected", None),
        _result(1, 0, "qualified", None, "operational"),
        _result(2, 0, "qualified", "qualified", None),
    )
    candidate_results = (
        _result(2, 0, "qualified", "qualified", None),
        _result(1, 0, "qualified", "qualified", None),
        _result(0, 2, "rejected", "rejected", None),
        _result(0, 1, "rejected", "rejected", None),
        _result(0, 0, "rejected", "rejected", None),
    )
    baseline = _run("b" * 64, "c" * 64, manifest, baseline_results)
    candidate = _run("d" * 64, "e" * 64, manifest, candidate_results)

    comparison = compare_runs(manifest, baseline, candidate)

    assert baseline.target is not None
    assert candidate.target is not None
    assert baseline.target.prompt_release_id == candidate.target.prompt_release_id
    assert comparison.baseline_target == baseline.target
    assert comparison.candidate_target == candidate.target
    assert comparison.improvement_count == 2
    assert comparison.regression_count == 0
    assert [trial.transition for case in comparison.cases for trial in case.trials] == [
        "improvement",
        "unchanged",
        "unchanged",
        "improvement",
        "unchanged",
    ]
    assert compare_runs(manifest, baseline, candidate) == comparison
    with pytest.raises(ValueError, match="metrics must match"):
        compare_runs(
            manifest,
            baseline,
            candidate.model_copy(update={"metrics": baseline.metrics}),
        )
    with pytest.raises(ValueError, match="release targets must differ"):
        compare_runs(manifest, baseline, baseline.model_copy(update={"id": "f" * 64}))


def _manifest() -> EvaluationManifest:
    return EvaluationManifest(
        id="a" * 64,
        policy=ManifestPolicy(),
        cases=(
            _case(0, "rejected", True, 3),
            _case(1, "qualified", False, 1),
            _case(2, "qualified", False, 1),
        ),
        created_at=NOW,
        created_by="test",
    )


def _case(
    position: int,
    expected: str,
    critical: bool,
    trial_count: int,
) -> EvaluationManifestCase:
    return EvaluationManifestCase.model_validate(
        {
            "position": position,
            "curation_id": UUID(int=position + 1),
            "review_event_id": UUID(int=position + 10),
            "expected_outcome": expected,
            "critical": critical,
            "trial_count": trial_count,
            "input": EvaluationCaseInput(
                title="Engineer",
                company="Acme",
                url=f"https://example.com/{position}",
                source="other",
                description="Build useful tools.",
                location="Remote",
                keywords=("python",),
                date_posted=date(2026, 9, 10),
                observed_at=NOW,
                original_outcome="qualified",
                review_decision="pursue",
                target_profile="applied-ai-product-engineer",
            ),
        }
    )


def _result(
    position: int,
    trial: int,
    expected: str,
    actual: str | None,
    failure: str | None,
) -> EvaluationTrialResult:
    return EvaluationTrialResult.model_validate(
        {
            "id": f"{position * 10 + trial + 1:064x}",
            "case_position": position,
            "trial_index": trial,
            "expected_outcome": expected,
            "actual_outcome": actual,
            "failure_kind": failure,
            "reason": "Fixture result.",
        }
    )


def _run(
    run_id: str,
    relevance_release_id: str,
    manifest: EvaluationManifest,
    results: tuple[EvaluationTrialResult, ...],
) -> EvaluationRun:
    prompt_release_id = PromptReleaseId("9" * 64)
    return EvaluationRun(
        id=run_id,
        idempotency_key=f"run:{run_id}",
        manifest_id=manifest.id,
        prompt_release_id=prompt_release_id,
        target=ReleaseTarget(
            prompt_release_id=prompt_release_id,
            relevance_release_id=RelevanceReleaseId(relevance_release_id),
        ),
        implementation_ref="test",
        metrics=score_results(manifest, results),
        results=results,
        completed_at=NOW,
    )
