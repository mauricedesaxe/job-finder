from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import JsonValue, TypeAdapter

from contracts.test_postgres_authority import (
    _connection,  # pyright: ignore[reportPrivateUsage]
    _seed_evaluation_execution_context,  # pyright: ignore[reportPrivateUsage]
    _store_default_qualification_target,  # pyright: ignore[reportPrivateUsage]
)
from job_finder.benchmarks.manifests import load_manifest
from job_finder.benchmarks.provider_attempts import (
    provider_attempt_evidence,
    store_provider_attempts,
)
from job_finder.benchmarks.qualification_activation import (
    ActivateQualificationTargetCommand,
    QualificationActivationError,
    activate_qualification_target,
    get_active_qualification_target,
)
from job_finder.benchmarks.qualification_evidence import (
    FixtureCase,
    Phase,
    PhaseFixtureSet,
    ProviderExperimentSettings,
    QualificationEvidence,
    QualificationEvidenceId,
    RelevanceExperimentInput,
    store_fixture_set,
    store_qualification_evidence,
    store_relevance_experiment_input,
)
from job_finder.benchmarks.qualification_promotions import (
    PromotionEvidenceSelection,
    preview_qualification_promotion,
    record_qualification_promotion_decision,
)
from job_finder.benchmarks.scoring import EvaluationTrialResult, score_results
from job_finder.config import PostgresContractSettings
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.implementation_artifacts import ImplementationArtifactId
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.models import (
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
    ModelRequestId,
    PromptReleaseId,
    PromptVersionId,
)
from job_finder.evaluation.prompt_releases import load_prompt_release
from job_finder.evaluation.qualification_components import (
    ComponentReleaseId,
    QualificationTargetContent,
    QualificationTargetId,
    qualification_target_id,
)
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)

_NOW = datetime(2026, 9, 26, tzinfo=UTC)
_COVERAGE = {
    "direct": True,
    "ats": True,
    "qualified": True,
    "rejected": True,
    "retry": True,
    "relevance": True,
    "enrichment": True,
    "deduplication": True,
}


@pytest.fixture
def authority_schema() -> Generator[str, None, None]:
    name = f"job_finder_first_activation_{uuid4().hex}"
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            _ = connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _attempt(
    prompt_release_id: PromptReleaseId,
    prompt_name: str,
    version_id: PromptVersionId,
) -> ModelCallAttempt:
    return ModelCallAttempt(
        id=uuid4(),
        context=ModelCallContext(
            processing_attempt_id=uuid4(),
            pipeline_run_id=uuid4(),
            prompt_release_id=prompt_release_id,
            operation_key=prompt_name,
            input_digest=InputDigest("a" * 64),
        ),
        request_id=ModelRequestId("b" * 64),
        attempt_number=0,
        prompt_name=prompt_name,
        prompt_version_id=version_id,
        requested_model="google/gemini-2.5-flash-001",
        response_model="google/gemini-2.5-flash-001",
        provider_response_id=str(uuid4()),
        status="accepted",
        parsed_output={"qualified": True},
        raw_response={"qualified": True},
        input_tokens=12,
        output_tokens=4,
        cost_usd=Decimal("0.00012"),
        latency_ms=2,
        error=None,
        observed_at=_NOW,
        request_messages=({"role": "user", "content": "source listing"},),
    )


def _evidence(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    target: QualificationTargetContent,
    artifact_id: ImplementationArtifactId,
    manifest_id: str,
    rates: ExchangeRateSnapshot,
    prompt_release_id: PromptReleaseId,
) -> PromotionEvidenceSelection:
    manifest = load_manifest(connection, manifest_id)
    frozen = RelevanceExperimentInput(
        manifest_id=manifest_id,
        exchange_rates=rates,
        provider_settings=ProviderExperimentSettings(
            provider="openrouter", temperature=0, seed=1, retry_limit=2
        ),
        input_path="direct",
    )
    frozen_id = store_relevance_experiment_input(
        connection, frozen, created_at=_NOW, created_by="owner"
    )
    trials = tuple(
        EvaluationTrialResult(
            id=uuid4().hex * 2,
            case_position=case.position,
            trial_index=index,
            expected_outcome=case.expected_outcome,
            actual_outcome=case.expected_outcome,
            failure_kind=None,
            reason="Matched expected result",
        )
        for case in manifest.cases
        for index in range(case.trial_count)
    )
    release = load_prompt_release(connection, prompt_release_id)
    version = release.versions[0]
    evidence_ids: dict[Phase, QualificationEvidenceId] = {}
    components: dict[Phase, ComponentReleaseId | None] = {
        "input_preparation": target.input_preparation_release_id,
        "relevance": target.relevance_release_id,
        "enrichment": target.enrichment_release_id,
        "deduplication": target.deduplication_release_id,
        "composition": None,
    }
    for phase, component_id in components.items():
        fixture_id = None
        if phase != "relevance":
            fixture_id = store_fixture_set(
                connection,
                PhaseFixtureSet(
                    phase=phase,
                    cases=(FixtureCase(input={}, expected={}, input_path="direct"),),
                ),
                created_at=_NOW,
                created_by="owner",
            )
        attempt = (
            None
            if phase == "input_preparation"
            else _attempt(prompt_release_id, version.definition.name, version.id)
        )
        result = TypeAdapter(dict[str, JsonValue]).validate_python(
            (
                {
                    "metrics": score_results(manifest, trials).model_dump(mode="json"),
                    "trials": [trial.model_dump(mode="json") for trial in trials],
                }
                if phase == "relevance"
                else {
                    "case_count": 1,
                    "passed_count": 1,
                    "cases": [{"passed": True, "observed": {}, "expected": {}}],
                    **({"coverage": _COVERAGE} if phase == "composition" else {}),
                }
            )
        )
        evidence = QualificationEvidence(
            target_id=target_id,
            phase=phase,
            component_release_id=component_id,
            experiment_input_id=frozen_id if phase == "relevance" else None,
            fixture_set_id=fixture_id,
            executor_artifact_id=artifact_id,
            origin="canonical",
            outcome="passed",
            result=result,
            attempts=(provider_attempt_evidence(attempt),) if attempt is not None else (),
            completed_at=_NOW,
        )
        evidence_ids[phase] = store_qualification_evidence(
            connection, evidence, created_at=_NOW, created_by="owner"
        )
        if attempt is not None:
            store_provider_attempts(
                connection,
                evidence,
                (attempt,),
                provider="openrouter",
                created_at=_NOW,
                created_by="owner",
            )
    return PromotionEvidenceSelection(
        input_preparation_evidence_id=evidence_ids["input_preparation"],
        relevance_evidence_id=evidence_ids["relevance"],
        enrichment_evidence_id=evidence_ids["enrichment"],
        deduplication_evidence_id=evidence_ids["deduplication"],
        composition_evidence_id=evidence_ids["composition"],
    )


def test_first_activation_requires_every_phase_and_exact_empty_baseline(
    authority_schema: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, prefix=".qualification-first-", suffix=".json") as file:
        artifact_path = Path(file.name)
        with _connection(authority_schema) as connection:
            manifest_id, _, rates = _seed_evaluation_execution_context(connection, _NOW)
            artifact, _, target = _store_default_qualification_target(connection, _NOW)
            candidate_id = qualification_target_id(target)
            assert write_implementation_artifact(root, artifact_path) == artifact
            prompt_release_id = bind_qualification_prompt_release(
                connection, candidate_id, artifact_path, created_at=_NOW, created_by="owner"
            )
            missing = preview_qualification_promotion(
                connection, None, candidate_id, PromotionEvidenceSelection(), artifact_path
            )
            assert not missing.eligible
            assert all(
                phase in " ".join(missing.failures)
                for phase in (
                    "input_preparation",
                    "relevance",
                    "enrichment",
                    "deduplication",
                    "Composition",
                )
            )
            selected = _evidence(
                connection,
                candidate_id,
                target,
                artifact.id,
                manifest_id,
                rates,
                prompt_release_id,
            )
            assert preview_qualification_promotion(
                connection, None, candidate_id, selected, artifact_path
            ).eligible
            missing_relevance = selected.model_copy(update={"relevance_evidence_id": None})
            assert not preview_qualification_promotion(
                connection, None, candidate_id, missing_relevance, artifact_path
            ).eligible
            assert not preview_qualification_promotion(
                connection,
                None,
                candidate_id,
                selected.model_copy(update={"relevance_comparison_id": "f" * 64}),
                artifact_path,
            ).eligible
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s",
                (selected.relevance_evidence_id,),
            ).fetchone()
            assert row is not None
            relevance_evidence = QualificationEvidence.model_validate(row[0])
            without_attempt = relevance_evidence.model_copy(
                update={"completed_at": _NOW + timedelta(seconds=1)}
            )
            without_attempt_id = store_qualification_evidence(
                connection, without_attempt, created_at=_NOW, created_by="owner"
            )
            no_provider = preview_qualification_promotion(
                connection,
                None,
                candidate_id,
                selected.model_copy(update={"relevance_evidence_id": without_attempt_id}),
                artifact_path,
            )
            assert "relevance provider attempts are missing or differ" in no_provider.failures
            synthetic = relevance_evidence.model_copy(update={"origin": "synthetic"})
            synthetic_id = store_qualification_evidence(
                connection, synthetic, created_at=_NOW, created_by="owner"
            )
            noncanonical = preview_qualification_promotion(
                connection,
                None,
                candidate_id,
                selected.model_copy(update={"relevance_evidence_id": synthetic_id}),
                artifact_path,
            )
            assert (
                "relevance evidence does not prove the candidate component" in noncanonical.failures
            )
            approved = record_qualification_promotion_decision(
                connection,
                baseline_target_id=None,
                candidate_target_id=candidate_id,
                evidence=selected,
                artifact_path=artifact_path,
                decision="approved",
                reason="All canonical phases passed",
                actor="owner",
                created_at=_NOW,
                idempotency_key="first-activation-decision",
            )
            assert approved.baseline_target_id is None
            assert (
                record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=None,
                    candidate_target_id=candidate_id,
                    evidence=selected,
                    artifact_path=artifact_path,
                    decision="approved",
                    reason="All canonical phases passed",
                    actor="owner",
                    created_at=_NOW,
                    idempotency_key="first-activation-decision",
                )
                == approved
            )
            with pytest.raises(ValueError, match="different promotion decision"):
                record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=None,
                    candidate_target_id=candidate_id,
                    evidence=missing_relevance,
                    artifact_path=artifact_path,
                    decision="approved",
                    reason="All canonical phases passed",
                    actor="owner",
                    created_at=_NOW,
                    idempotency_key="first-activation-decision",
                )
            with pytest.raises(ValueError, match="already has a promotion decision"):
                record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=None,
                    candidate_target_id=candidate_id,
                    evidence=selected,
                    artifact_path=artifact_path,
                    decision="approved",
                    reason="All canonical phases passed",
                    actor="owner",
                    created_at=_NOW,
                    idempotency_key="another-first-activation-decision",
                )
            with pytest.raises(psycopg.errors.UniqueViolation):
                _ = connection.execute(
                    """
                    INSERT INTO qualification_promotion_decisions (
                      id, idempotency_key, baseline_target_id, candidate_target_id,
                      composition_evidence_id, decision, reason, actor, created_at
                    ) VALUES (%s, %s, NULL, %s, %s, 'approved', %s, %s, %s)
                    """,
                    (
                        "e" * 64,
                        "duplicate-first-activation-in-sql",
                        candidate_id,
                        selected.composition_evidence_id,
                        "Duplicate pair",
                        "owner",
                        _NOW,
                    ),
                )
            command = ActivateQualificationTargetCommand(
                idempotency_key="first-activation",
                promotion_decision_id=approved.id,
                expected_target_id=None,
                expected_generation=0,
                actor="owner",
                timestamp=_NOW,
            )
            activated = activate_qualification_target(connection, command, artifact_path)
            assert activated.outcome == "activated"
            assert activated.baseline_target_id is None
            assert activated.resulting_generation == 1
            assert get_active_qualification_target(connection).target_id == candidate_id
            assert activate_qualification_target(connection, command, artifact_path).replayed
            with pytest.raises(QualificationActivationError, match="another activation request"):
                activate_qualification_target(
                    connection, command.model_copy(update={"actor": "other"}), artifact_path
                )
            stale = activate_qualification_target(
                connection,
                command.model_copy(update={"idempotency_key": "stale-first-activation"}),
                artifact_path,
            )
            assert stale.outcome == "active_changed"
            assert stale.resulting_generation == 1
