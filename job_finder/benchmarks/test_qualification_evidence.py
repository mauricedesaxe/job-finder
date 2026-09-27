from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast, final

import psycopg
import pytest
from pydantic import ValidationError

from job_finder.benchmarks.qualification_evidence import (
    FixtureCase,
    FixtureSetId,
    PhaseFixtureSet,
    ProviderExperimentSettings,
    QualificationEvidence,
    RelevanceExperimentInput,
    experiment_input_id,
    fixture_set_id,
    load_fixture_set,
    require_comparable_relevance_evidence,
)
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.qualification_components import ComponentReleaseId, QualificationTargetId
from job_finder.evaluation.implementation_artifacts import ImplementationArtifactId


@final
class _QueryResult:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


@final
class _FixtureConnection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def execute(self, _query: str, _params: tuple[object, ...]) -> _QueryResult:
        return _QueryResult(self._row)


def _fixture_connection(
    row: tuple[object, ...] | None,
) -> psycopg.Connection[tuple[object, ...]]:
    return cast(psycopg.Connection[tuple[object, ...]], cast(object, _FixtureConnection(row)))


def _fixture_set() -> PhaseFixtureSet:
    return PhaseFixtureSet(
        phase="enrichment",
        cases=(FixtureCase(input={}, expected={}, input_path="direct"),),
    )


def test_load_fixture_set_checks_stored_content_identity() -> None:
    fixture = _fixture_set()
    identity = fixture_set_id(fixture)

    assert (
        load_fixture_set(
            _fixture_connection(("enrichment", fixture.model_dump(mode="json"))),
            identity,
            phase="enrichment",
        )
        == fixture
    )

    with pytest.raises(ValueError, match="invalid identity"):
        _ = load_fixture_set(
            _fixture_connection(("composition", fixture.model_dump(mode="json"))),
            identity,
            phase="enrichment",
        )
    with pytest.raises(ValueError, match="invalid identity"):
        _ = load_fixture_set(
            _fixture_connection(("enrichment", fixture.model_dump(mode="json"))),
            FixtureSetId("0" * 64),
            phase="enrichment",
        )


def test_load_fixture_set_rejects_missing_content() -> None:
    with pytest.raises(ValueError, match="Enrichment fixture set not found"):
        _ = load_fixture_set(_fixture_connection(None), FixtureSetId("0" * 64), phase="enrichment")


def test_relevance_comparison_uses_one_frozen_input() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    experiment = RelevanceExperimentInput(
        manifest_id="a" * 64,
        exchange_rates=ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.10")}, source="fallback", observed_at=now
        ),
        provider_settings=ProviderExperimentSettings(
            provider="openrouter", temperature=0, seed=7, retry_limit=2
        ),
        input_path="direct",
    )
    frozen_id = experiment_input_id(experiment)
    baseline = QualificationEvidence(
        target_id=QualificationTargetId("b" * 64),
        phase="relevance",
        component_release_id=ComponentReleaseId("c" * 64),
        experiment_input_id=frozen_id,
        executor_artifact_id=ImplementationArtifactId("d" * 64),
        origin="canonical",
        outcome="passed",
        result={"qualified": 3},
        completed_at=now,
    )
    candidate = baseline.model_copy(update={"target_id": QualificationTargetId("e" * 64)})
    assert require_comparable_relevance_evidence(baseline, candidate) == frozen_id

    changed_rates = experiment.model_copy(
        update={
            "exchange_rates": experiment.exchange_rates.model_copy(
                update={"rates": {"EUR": Decimal("1.11")}}
            )
        }
    )
    assert experiment_input_id(changed_rates) != frozen_id
    with pytest.raises(ValueError, match="same frozen experiment input"):
        _ = require_comparable_relevance_evidence(
            baseline,
            candidate.model_copy(
                update={"experiment_input_id": experiment_input_id(changed_rates)}
            ),
        )
    with pytest.raises(ValidationError, match="frozen fixture set"):
        _ = QualificationEvidence.model_validate(
            baseline.model_dump(mode="json") | {"phase": "composition"}
        )
