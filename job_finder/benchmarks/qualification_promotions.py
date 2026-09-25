from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from job_finder.benchmarks.comparisons import promotion_eligibility_failures
from job_finder.benchmarks.identity import canonical_digest
from job_finder.benchmarks.manifests import load_manifest
from job_finder.benchmarks.provider_attempts import provider_attempt_evidence
from job_finder.benchmarks.qualification_evidence import (
    Phase,
    PhaseFixtureSet,
    QualificationEvidence,
    QualificationEvidenceId,
    RelevanceExperimentInput,
    experiment_input_id,
    fixture_set_id,
    qualification_evidence_id,
)
from job_finder.benchmarks.scoring import EvaluationMetrics, EvaluationTrialResult, score_results
from job_finder.evaluation.models import ModelCallAttempt
from job_finder.evaluation.qualification_components import (
    QualificationTargetId,
    ResolvedQualificationTarget,
    load_qualification_target,
    resolve_executable_qualification_target,
)

_DIGEST = r"^[0-9a-f]{64}$"
_ATTEMPT: TypeAdapter[ModelCallAttempt] = TypeAdapter(ModelCallAttempt)
_TRIALS: TypeAdapter[tuple[EvaluationTrialResult, ...]] = TypeAdapter(
    tuple[EvaluationTrialResult, ...]
)
_CASES: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])
_REQUIRED_COMPOSITION_COVERAGE = frozenset(
    {
        "direct",
        "ats",
        "qualified",
        "rejected",
        "retry",
        "relevance",
        "enrichment",
        "deduplication",
    }
)


class PromotionEvidenceSelection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    input_preparation_evidence_id: Annotated[
        QualificationEvidenceId | None, Field(pattern=_DIGEST)
    ] = None
    relevance_evidence_id: Annotated[QualificationEvidenceId | None, Field(pattern=_DIGEST)] = None
    enrichment_evidence_id: Annotated[QualificationEvidenceId | None, Field(pattern=_DIGEST)] = None
    deduplication_evidence_id: Annotated[QualificationEvidenceId | None, Field(pattern=_DIGEST)] = (
        None
    )
    composition_evidence_id: Annotated[QualificationEvidenceId | None, Field(pattern=_DIGEST)] = (
        None
    )
    relevance_comparison_id: Annotated[str | None, Field(pattern=_DIGEST)] = None


class QualificationPromotionPreview(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    baseline_target_id: QualificationTargetId
    candidate_target_id: QualificationTargetId
    evidence: PromotionEvidenceSelection
    eligible: bool
    failures: tuple[str, ...]


class QualificationPromotionDecision(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=_DIGEST)]
    baseline_target_id: QualificationTargetId
    candidate_target_id: QualificationTargetId
    evidence: PromotionEvidenceSelection
    decision: Literal["approved", "rejected"]
    reason: str
    actor: str
    created_at: datetime


def record_qualification_promotion_decision(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    baseline_target_id: QualificationTargetId,
    candidate_target_id: QualificationTargetId,
    evidence: PromotionEvidenceSelection,
    artifact_path: Path,
    decision: Literal["approved", "rejected"],
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> QualificationPromotionDecision:
    _validate_promotion_request(
        connection, baseline_target_id, candidate_target_id, idempotency_key, reason, actor
    )
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"qualification_promotion_key:{idempotency_key}",),
        )
        existing = load_qualification_promotion_decision(connection, idempotency_key)
        if existing is not None:
            if not _same_promotion_request(
                existing, baseline_target_id, candidate_target_id, evidence, decision, reason, actor
            ):
                raise ValueError("Idempotency key belongs to a different promotion decision")
            return existing
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"qualification_promotion_pair:{baseline_target_id}:{candidate_target_id}",),
        )
        duplicate = connection.execute(
            "SELECT 1 FROM qualification_promotion_decisions WHERE baseline_target_id = %s AND candidate_target_id = %s",
            (baseline_target_id, candidate_target_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("This target pair already has a promotion decision")
        preview = preview_qualification_promotion(
            connection, baseline_target_id, candidate_target_id, evidence, artifact_path
        )
        if decision == "approved" and not preview.eligible:
            raise ValueError(
                "Ineligible qualification target cannot be approved: " + "; ".join(preview.failures)
            )
        promotion = QualificationPromotionDecision(
            id=canonical_digest(
                {"kind": "qualification_promotion", "idempotency_key": idempotency_key}
            ),
            baseline_target_id=baseline_target_id,
            candidate_target_id=candidate_target_id,
            evidence=evidence,
            decision=decision,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _ = connection.execute(
            """
            INSERT INTO qualification_promotion_decisions (
              id, idempotency_key, baseline_target_id, candidate_target_id,
              input_preparation_evidence_id, relevance_evidence_id,
              enrichment_evidence_id, deduplication_evidence_id,
              composition_evidence_id, relevance_comparison_id, decision,
              reason, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                promotion.id,
                idempotency_key,
                baseline_target_id,
                candidate_target_id,
                evidence.input_preparation_evidence_id,
                evidence.relevance_evidence_id,
                evidence.enrichment_evidence_id,
                evidence.deduplication_evidence_id,
                evidence.composition_evidence_id,
                evidence.relevance_comparison_id,
                decision,
                reason,
                actor,
                created_at,
            ),
        )
    return promotion


def _validate_promotion_request(
    connection: psycopg.Connection[tuple[object, ...]],
    baseline_target_id: QualificationTargetId,
    candidate_target_id: QualificationTargetId,
    idempotency_key: str,
    reason: str,
    actor: str,
) -> None:
    if not connection.autocommit:
        raise ValueError("Promotion decisions require an autocommit connection")
    if not idempotency_key or not reason.strip() or not actor.strip():
        raise ValueError("Promotion key, reason, and actor must not be blank")
    if baseline_target_id == candidate_target_id:
        raise ValueError("Candidate is the baseline target")


def _same_promotion_request(
    existing: QualificationPromotionDecision,
    baseline_target_id: QualificationTargetId,
    candidate_target_id: QualificationTargetId,
    evidence: PromotionEvidenceSelection,
    decision: Literal["approved", "rejected"],
    reason: str,
    actor: str,
) -> bool:
    return (
        existing.baseline_target_id,
        existing.candidate_target_id,
        existing.evidence,
        existing.decision,
        existing.reason,
        existing.actor,
    ) == (baseline_target_id, candidate_target_id, evidence, decision, reason, actor)


def load_qualification_promotion_decision(
    connection: psycopg.Connection[tuple[object, ...]], idempotency_key: str
) -> QualificationPromotionDecision | None:
    row = connection.execute(
        """
        SELECT id, baseline_target_id, candidate_target_id,
               input_preparation_evidence_id, relevance_evidence_id,
               enrichment_evidence_id, deduplication_evidence_id,
               composition_evidence_id, relevance_comparison_id, decision,
               reason, actor, created_at
        FROM qualification_promotion_decisions WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return QualificationPromotionDecision.model_validate(
        {
            "id": row[0],
            "baseline_target_id": row[1],
            "candidate_target_id": row[2],
            "evidence": {
                "input_preparation_evidence_id": row[3],
                "relevance_evidence_id": row[4],
                "enrichment_evidence_id": row[5],
                "deduplication_evidence_id": row[6],
                "composition_evidence_id": row[7],
                "relevance_comparison_id": row[8],
            },
            "decision": row[9],
            "reason": row[10],
            "actor": row[11],
            "created_at": row[12],
        }
    )


def preview_qualification_promotion(
    connection: psycopg.Connection[tuple[object, ...]],
    baseline_target_id: QualificationTargetId,
    candidate_target_id: QualificationTargetId,
    evidence: PromotionEvidenceSelection,
    artifact_path: Path,
) -> QualificationPromotionPreview:
    baseline = load_qualification_target(connection, baseline_target_id)
    candidate = resolve_executable_qualification_target(
        connection, candidate_target_id, artifact_path
    )
    failures: list[str] = []
    if baseline.id == candidate.id:
        failures.append("Candidate is the baseline target")
    components: tuple[tuple[Phase, str, str, QualificationEvidenceId | None], ...] = (
        (
            "input_preparation",
            baseline.content.input_preparation_release_id,
            candidate.content.input_preparation_release_id,
            evidence.input_preparation_evidence_id,
        ),
        (
            "relevance",
            baseline.content.relevance_release_id,
            candidate.content.relevance_release_id,
            evidence.relevance_evidence_id,
        ),
        (
            "enrichment",
            baseline.content.enrichment_release_id,
            candidate.content.enrichment_release_id,
            evidence.enrichment_evidence_id,
        ),
        (
            "deduplication",
            baseline.content.deduplication_release_id,
            candidate.content.deduplication_release_id,
            evidence.deduplication_evidence_id,
        ),
    )
    for phase, baseline_component, candidate_component, selected_id in components:
        _check_component_evidence(
            connection,
            phase,
            baseline_component != candidate_component,
            candidate_component,
            selected_id,
            baseline,
            candidate,
            failures,
        )
    _check_composition(connection, evidence.composition_evidence_id, candidate, failures)
    relevance_changed = (
        baseline.content.relevance_release_id != candidate.content.relevance_release_id
    )
    _check_relevance_comparison(
        connection, baseline, candidate, evidence, relevance_changed, failures
    )
    return QualificationPromotionPreview(
        baseline_target_id=baseline.id,
        candidate_target_id=candidate.id,
        evidence=evidence,
        eligible=not failures,
        failures=tuple(failures),
    )


def _check_component_evidence(
    connection: psycopg.Connection[tuple[object, ...]],
    phase: Phase,
    changed: bool,
    component_id: str,
    evidence_id: QualificationEvidenceId | None,
    baseline: ResolvedQualificationTarget,
    candidate: ResolvedQualificationTarget,
    failures: list[str],
) -> None:
    if evidence_id is None:
        if changed:
            failures.append(f"Changed {phase} component has no evidence")
        return
    item = _load_evidence(connection, evidence_id)
    if item is None:
        failures.append(f"{phase} evidence is missing or has invalid identity")
        return
    allowed_targets = {candidate.id} if changed else {baseline.id, candidate.id}
    if not _evidence_matches_component(
        item, phase, allowed_targets, component_id, candidate.content.artifact_id
    ):
        failures.append(f"{phase} evidence does not prove the candidate component")
        return
    _check_evidence_result(connection, item, failures)


def _evidence_matches_component(
    item: QualificationEvidence,
    phase: Phase,
    allowed_targets: set[QualificationTargetId],
    component_id: str,
    artifact_id: str,
) -> bool:
    return (
        item.phase == phase
        and item.target_id in allowed_targets
        and item.component_release_id == component_id
        and item.executor_artifact_id == artifact_id
        and item.origin == "canonical"
        and item.outcome == "passed"
    )


def _check_composition(
    connection: psycopg.Connection[tuple[object, ...]],
    evidence_id: QualificationEvidenceId | None,
    candidate: ResolvedQualificationTarget,
    failures: list[str],
) -> None:
    if evidence_id is None:
        failures.append("Composition evidence is missing")
        return
    item = _load_evidence(connection, evidence_id)
    if item is None:
        failures.append("Composition evidence is missing or has invalid identity")
        return
    if not _composition_matches_target(item, candidate):
        failures.append("Composition evidence does not prove the candidate target")
        return
    coverage = item.result.get("coverage")
    if not _complete_coverage(coverage):
        failures.append("Composition evidence lacks full production path coverage")
    _check_evidence_result(connection, item, failures)


def _composition_matches_target(
    item: QualificationEvidence, candidate: ResolvedQualificationTarget
) -> bool:
    return (
        item.phase == "composition"
        and item.target_id == candidate.id
        and item.executor_artifact_id == candidate.content.artifact_id
        and item.origin == "canonical"
        and item.outcome == "passed"
    )


def _complete_coverage(coverage: JsonValue | None) -> bool:
    return isinstance(coverage, dict) and all(
        coverage.get(key) is True for key in _REQUIRED_COMPOSITION_COVERAGE
    )


def _check_evidence_result(
    connection: psycopg.Connection[tuple[object, ...]],
    item: QualificationEvidence,
    failures: list[str],
) -> None:
    if item.phase == "relevance":
        _check_relevance_result(connection, item, failures)
    else:
        _check_fixture_result(connection, item, failures)
    _check_provider_attempts(connection, item, failures)


def _check_fixture_result(
    connection: psycopg.Connection[tuple[object, ...]],
    item: QualificationEvidence,
    failures: list[str],
) -> None:
    row = connection.execute(
        "SELECT content FROM qualification_fixture_sets WHERE id = %s",
        (item.fixture_set_id,),
    ).fetchone()
    if row is None:
        failures.append(f"{item.phase} fixture set is missing")
        return
    fixture = PhaseFixtureSet.model_validate(row[0])
    if fixture_set_id(fixture) != item.fixture_set_id or fixture.phase != item.phase:
        failures.append(f"{item.phase} fixture identity differs from evidence")
        return
    if not _fixture_results_complete(item, fixture):
        failures.append(f"{item.phase} fixture results are incomplete")


def _fixture_results_complete(item: QualificationEvidence, fixture: PhaseFixtureSet) -> bool:
    try:
        cases = _CASES.validate_python(item.result.get("cases"))
    except ValueError:
        return False
    count = len(fixture.cases)
    return (
        item.result.get("case_count") == count
        and item.result.get("passed_count") == count
        and len(cases) == count
        and all(
            case.get("passed") is True and case.get("observed") == case.get("expected")
            for case in cases
        )
    )


def _check_relevance_result(
    connection: psycopg.Connection[tuple[object, ...]],
    item: QualificationEvidence,
    failures: list[str],
) -> None:
    row = connection.execute(
        "SELECT content FROM relevance_experiment_inputs WHERE id = %s",
        (item.experiment_input_id,),
    ).fetchone()
    if row is None:
        failures.append("Relevance experiment input is missing")
        return
    frozen = RelevanceExperimentInput.model_validate(row[0])
    if experiment_input_id(frozen) != item.experiment_input_id:
        failures.append("Relevance experiment input has invalid identity")
        return
    manifest = load_manifest(connection, frozen.manifest_id)
    try:
        trials = _TRIALS.validate_python(item.result.get("trials"))
        metrics = EvaluationMetrics.model_validate(item.result.get("metrics"))
        matches = score_results(manifest, trials) == metrics
    except ValueError:
        matches = False
    if not matches:
        failures.append("Relevance metrics differ from frozen trial results")


def _check_provider_attempts(
    connection: psycopg.Connection[tuple[object, ...]],
    item: QualificationEvidence,
    failures: list[str],
) -> None:
    rows = connection.execute(
        "SELECT content FROM qualification_provider_attempts WHERE evidence_id = %s ORDER BY created_at, id",
        (qualification_evidence_id(item),),
    ).fetchall()
    try:
        attempts = tuple(_ATTEMPT.validate_python(row[0]) for row in rows)
    except ValueError:
        failures.append(f"{item.phase} provider attempts have invalid content")
        return
    if item.phase == "input_preparation":
        if attempts or item.attempts:
            failures.append("Input preparation evidence unexpectedly has provider attempts")
    elif not _attempts_match_evidence(attempts, item):
        failures.append(f"{item.phase} provider attempts are missing or differ")


def _attempts_match_evidence(
    attempts: tuple[ModelCallAttempt, ...], item: QualificationEvidence
) -> bool:
    summaries = tuple(provider_attempt_evidence(attempt) for attempt in attempts)
    return bool(attempts) and sorted(summary.model_dump_json() for summary in summaries) == (
        sorted(summary.model_dump_json() for summary in item.attempts)
    )


def _check_relevance_comparison(
    connection: psycopg.Connection[tuple[object, ...]],
    baseline: ResolvedQualificationTarget,
    candidate: ResolvedQualificationTarget,
    selected: PromotionEvidenceSelection,
    relevance_changed: bool,
    failures: list[str],
) -> None:
    if not relevance_changed:
        if selected.relevance_comparison_id is not None:
            failures.append("Unchanged relevance does not need a comparison")
        return
    if selected.relevance_comparison_id is None or selected.relevance_evidence_id is None:
        failures.append("Changed relevance requires a frozen comparison")
        return
    compared = _load_comparison_evidence(connection, baseline, candidate, selected, failures)
    if compared is None:
        return
    baseline_item, candidate_item, experiment_id = compared
    _check_evidence_result(connection, baseline_item, failures)
    frozen_row = connection.execute(
        "SELECT content FROM relevance_experiment_inputs WHERE id = %s", (experiment_id,)
    ).fetchone()
    if frozen_row is None:
        failures.append("Frozen relevance comparison input is missing")
        return
    frozen = RelevanceExperimentInput.model_validate(frozen_row[0])
    manifest = load_manifest(connection, frozen.manifest_id)
    try:
        baseline_metrics = EvaluationMetrics.model_validate(baseline_item.result.get("metrics"))
        candidate_metrics = EvaluationMetrics.model_validate(candidate_item.result.get("metrics"))
    except ValueError:
        failures.append("Relevance comparison metrics are invalid")
        return
    failures.extend(
        promotion_eligibility_failures(manifest.policy, baseline_metrics, candidate_metrics)
    )


def _load_comparison_evidence(
    connection: psycopg.Connection[tuple[object, ...]],
    baseline: ResolvedQualificationTarget,
    candidate: ResolvedQualificationTarget,
    selected: PromotionEvidenceSelection,
    failures: list[str],
) -> tuple[QualificationEvidence, QualificationEvidence, str] | None:
    row = connection.execute(
        """
        SELECT experiment_input_id, baseline_evidence_id, candidate_evidence_id
        FROM qualification_relevance_comparisons WHERE id = %s
        """,
        (selected.relevance_comparison_id,),
    ).fetchone()
    if row is None or row[2] != selected.relevance_evidence_id:
        failures.append("Relevance comparison is missing or selects another candidate")
        return None
    baseline_item = _load_evidence(connection, QualificationEvidenceId(str(row[1])))
    candidate_item = _load_evidence(connection, QualificationEvidenceId(str(row[2])))
    if (
        baseline_item is None
        or candidate_item is None
        or not _comparison_evidence_matches(
            baseline_item, candidate_item, baseline, candidate, str(row[0])
        )
    ):
        failures.append("Relevance comparison lacks exact baseline and candidate evidence")
        return None
    return baseline_item, candidate_item, str(row[0])


def _comparison_evidence_matches(
    baseline_item: QualificationEvidence,
    candidate_item: QualificationEvidence,
    baseline: ResolvedQualificationTarget,
    candidate: ResolvedQualificationTarget,
    experiment_id: str,
) -> bool:
    return (
        baseline_item.target_id == baseline.id
        and baseline_item.component_release_id == baseline.content.relevance_release_id
        and baseline_item.executor_artifact_id == baseline.content.artifact_id
        and baseline_item.origin == "canonical"
        and baseline_item.outcome == "passed"
        and baseline_item.experiment_input_id == experiment_id
        and candidate_item.target_id == candidate.id
        and candidate_item.component_release_id == candidate.content.relevance_release_id
        and candidate_item.experiment_input_id == experiment_id
    )


def _load_evidence(
    connection: psycopg.Connection[tuple[object, ...]], evidence_id: QualificationEvidenceId
) -> QualificationEvidence | None:
    row = connection.execute(
        "SELECT content FROM qualification_phase_evidence WHERE id = %s", (evidence_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        item = QualificationEvidence.model_validate(row[0])
    except ValueError:
        return None
    return item if qualification_evidence_id(item) == evidence_id else None
