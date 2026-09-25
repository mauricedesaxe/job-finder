from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from job_finder.benchmarks.qualification_evidence import (
    ProviderExperimentSettings,
    QualificationEvidence,
    RelevanceExperimentInput,
    experiment_input_id,
    require_comparable_relevance_evidence,
)
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.qualification_components import ComponentReleaseId, QualificationTargetId
from job_finder.evaluation.implementation_artifacts import ImplementationArtifactId


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
