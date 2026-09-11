from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_finder.evaluation.models import (
    EvaluationResult,
    OperationalError,
    PromptReleaseId,
    Qualified,
)

Digest = str
ExpectedOutcome = Literal["qualified", "rejected"]
Connection = psycopg.Connection[tuple[object, ...]]


class ManifestModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ManifestPolicy(ManifestModel):
    regular_trial_count: int = Field(default=1, gt=0)
    critical_trial_count: int = Field(default=3, gt=1)
    max_false_positive_rate: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    max_false_negative_rate: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)

    @model_validator(mode="after")
    def thresholds_and_trials_are_ordered(self) -> Self:
        if self.critical_trial_count <= self.regular_trial_count:
            raise ValueError("Critical cases must run more trials than regular cases")
        if self.max_false_positive_rate >= self.max_false_negative_rate:
            raise ValueError("The false-positive threshold must be stricter")
        return self


class EvaluationCaseInput(ManifestModel):
    title: str
    company: str
    url: str
    source: str
    description: str
    location: str
    keywords: tuple[str, ...]
    date_posted: date | None
    observed_at: datetime
    original_outcome: ExpectedOutcome
    review_decision: Literal["pursue", "reject"]
    target_profile: str | None


class CuratedReviewEvent(ManifestModel):
    id: UUID
    review_event_id: UUID
    action: Literal["include", "exclude"]
    expected_outcome: ExpectedOutcome | None
    critical: bool
    reason: str
    actor: str
    created_at: datetime

    @model_validator(mode="after")
    def action_has_valid_fields(self) -> Self:
        if self.action == "include" and self.expected_outcome is None:
            raise ValueError("Included feedback requires an expected outcome")
        if self.action == "exclude" and (self.expected_outcome is not None or self.critical):
            raise ValueError("Excluded feedback cannot define evaluation behavior")
        return self


class EvaluationManifestCase(ManifestModel):
    position: int = Field(ge=0)
    curation_id: UUID
    review_event_id: UUID
    expected_outcome: ExpectedOutcome
    critical: bool
    trial_count: int = Field(gt=0)
    input: EvaluationCaseInput


class EvaluationManifest(ManifestModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ManifestPolicy
    cases: tuple[EvaluationManifestCase, ...]
    created_at: datetime
    created_by: str

    @model_validator(mode="after")
    def cases_follow_policy(self) -> Self:
        if not self.cases:
            raise ValueError("An evaluation manifest requires at least one case")
        for position, case in enumerate(self.cases):
            expected_trials = (
                self.policy.critical_trial_count
                if case.critical
                else self.policy.regular_trial_count
            )
            if case.position != position or case.trial_count != expected_trials:
                raise ValueError("Manifest cases must be ordered and follow the trial policy")
        return self


class EvaluationTrialResult(ManifestModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_position: int = Field(ge=0)
    trial_index: int = Field(ge=0)
    expected_outcome: ExpectedOutcome
    actual_outcome: ExpectedOutcome | None
    failure_kind: Literal["false_positive", "false_negative", "operational"] | None
    reason: str

    @model_validator(mode="after")
    def classification_matches_outcomes(self) -> Self:
        expected_failure = _failure_kind(self.expected_outcome, self.actual_outcome)
        if self.failure_kind != expected_failure:
            raise ValueError("Trial failure kind must match its expected and actual outcomes")
        return self


class EvaluationMetrics(ManifestModel):
    result_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    operational_failure_count: int = Field(ge=0)
    critical_false_positive_count: int = Field(ge=0)
    false_positive_rate: Decimal = Field(ge=0, le=1)
    false_negative_rate: Decimal = Field(ge=0, le=1)


class EvaluationRun(ManifestModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_ref: str
    metrics: EvaluationMetrics
    results: tuple[EvaluationTrialResult, ...]
    completed_at: datetime


class PromptPromotionDecision(ManifestModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    reason: str
    actor: str
    created_at: datetime


CaseEvaluator = Callable[
    [EvaluationManifestCase, PromptReleaseId, int],
    EvaluationResult,
]


def include_review_event(
    connection: Connection,
    *,
    review_event_id: UUID,
    critical: bool,
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> CuratedReviewEvent:
    _require_autocommit(connection)
    with connection.transaction():
        existing = _load_curation_by_key(connection, idempotency_key)
        if existing is not None:
            _require_matching_curation(
                existing, review_event_id, "include", critical, reason, actor
            )
            return existing
        row = connection.execute(
            "SELECT decision FROM review_events WHERE id = %s",
            (review_event_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Review event does not exist")
        decision = str(row[0])
        if decision == "unsure":
            raise ValueError("Unsure feedback cannot define an evaluation expectation")
        expected: ExpectedOutcome = "qualified" if decision == "pursue" else "rejected"
        curation = CuratedReviewEvent(
            id=uuid5(NAMESPACE_URL, f"evaluation-curation:{idempotency_key}"),
            review_event_id=review_event_id,
            action="include",
            expected_outcome=expected,
            critical=critical,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _insert_curation(connection, idempotency_key, curation)
        return curation


def exclude_review_event(
    connection: Connection,
    *,
    review_event_id: UUID,
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> CuratedReviewEvent:
    _require_autocommit(connection)
    with connection.transaction():
        existing = _load_curation_by_key(connection, idempotency_key)
        if existing is not None:
            _require_matching_curation(existing, review_event_id, "exclude", False, reason, actor)
            return existing
        exists = connection.execute(
            "SELECT 1 FROM review_events WHERE id = %s",
            (review_event_id,),
        ).fetchone()
        if exists is None:
            raise ValueError("Review event does not exist")
        curation = CuratedReviewEvent(
            id=uuid5(NAMESPACE_URL, f"evaluation-curation:{idempotency_key}"),
            review_event_id=review_event_id,
            action="exclude",
            expected_outcome=None,
            critical=False,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _insert_curation(connection, idempotency_key, curation)
        return curation


def create_manifest(
    connection: Connection,
    *,
    policy: ManifestPolicy,
    created_at: datetime,
    created_by: str,
) -> EvaluationManifest:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute("LOCK TABLE evaluation_case_curations IN SHARE MODE")
        cases = _load_current_cases(connection, policy)
        if not cases:
            raise ValueError("An evaluation manifest requires at least one included case")
        content = {
            "policy": policy.model_dump(mode="json"),
            "cases": [case.model_dump(mode="json") for case in cases],
        }
        digest = _digest(content)
        inserted = connection.execute(
            """
            INSERT INTO evaluation_manifests (
              id, content_digest, expected_case_count, regular_trial_count,
              critical_trial_count, max_false_positive_rate,
              max_false_negative_rate, created_at, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_digest) DO NOTHING
            RETURNING id
            """,
            (
                digest,
                digest,
                len(cases),
                policy.regular_trial_count,
                policy.critical_trial_count,
                policy.max_false_positive_rate,
                policy.max_false_negative_rate,
                created_at,
                created_by,
            ),
        ).fetchone()
        if inserted is None:
            return load_manifest(connection, digest)
        for case in cases:
            _ = connection.execute(
                """
                INSERT INTO evaluation_manifest_cases (
                  manifest_id, position, curation_id, review_event_id,
                  expected_outcome, critical, trial_count, input
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    digest,
                    case.position,
                    case.curation_id,
                    case.review_event_id,
                    case.expected_outcome,
                    case.critical,
                    case.trial_count,
                    Jsonb(case.input.model_dump(mode="json")),
                ),
            )
        manifest = EvaluationManifest(
            id=digest,
            policy=policy,
            cases=cases,
            created_at=created_at,
            created_by=created_by,
        )
        enqueue_projection(connection, "evaluation_manifest", digest, manifest, created_at)
        return manifest


def load_manifest(connection: Connection, manifest_id: Digest) -> EvaluationManifest:
    row = connection.execute(
        """
        SELECT regular_trial_count, critical_trial_count, max_false_positive_rate,
               max_false_negative_rate, created_at, created_by
        FROM evaluation_manifests WHERE id = %s
        """,
        (manifest_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Evaluation manifest does not exist")
    case_rows = connection.execute(
        """
        SELECT position, curation_id, review_event_id, expected_outcome,
               critical, trial_count, input
        FROM evaluation_manifest_cases
        WHERE manifest_id = %s ORDER BY position
        """,
        (manifest_id,),
    ).fetchall()
    return EvaluationManifest(
        id=manifest_id,
        policy=ManifestPolicy(
            regular_trial_count=int(str(row[0])),
            critical_trial_count=int(str(row[1])),
            max_false_positive_rate=Decimal(str(row[2])),
            max_false_negative_rate=Decimal(str(row[3])),
        ),
        cases=tuple(
            EvaluationManifestCase.model_validate(
                {
                    "position": case[0],
                    "curation_id": case[1],
                    "review_event_id": case[2],
                    "expected_outcome": case[3],
                    "critical": case[4],
                    "trial_count": case[5],
                    "input": case[6],
                }
            )
            for case in case_rows
        ),
        created_at=datetime.fromisoformat(str(row[4])),
        created_by=str(row[5]),
    )


def run_manifest(
    connection: Connection,
    *,
    manifest_id: Digest,
    prompt_release_id: PromptReleaseId,
    evaluator: CaseEvaluator,
    implementation_ref: str,
    completed_at: datetime,
    idempotency_key: str,
) -> EvaluationRun:
    _require_autocommit(connection)
    existing = load_run_by_key(connection, idempotency_key)
    if existing is not None:
        if (
            existing.manifest_id != manifest_id
            or existing.prompt_release_id != prompt_release_id
            or existing.implementation_ref != implementation_ref
        ):
            raise ValueError("Idempotency key belongs to a different evaluation run")
        return existing
    if (
        connection.execute(
            "SELECT 1 FROM prompt_releases WHERE id = %s", (prompt_release_id,)
        ).fetchone()
        is None
    ):
        raise ValueError("Prompt release does not exist")
    manifest = load_manifest(connection, manifest_id)
    run_id = _digest({"kind": "evaluation_run", "idempotency_key": idempotency_key})
    results = tuple(
        _trial_result(run_id, case, trial_index, evaluator(case, prompt_release_id, trial_index))
        for case in manifest.cases
        for trial_index in range(case.trial_count)
    )
    metrics = score_results(manifest, results)
    run = EvaluationRun(
        id=run_id,
        idempotency_key=idempotency_key,
        manifest_id=manifest_id,
        prompt_release_id=prompt_release_id,
        implementation_ref=implementation_ref,
        metrics=metrics,
        results=results,
        completed_at=completed_at,
    )
    with connection.transaction():
        _insert_run(connection, run)
        enqueue_projection(connection, "evaluation_run", run.id, run, completed_at)
    return run


def score_results(
    manifest: EvaluationManifest,
    results: tuple[EvaluationTrialResult, ...],
) -> EvaluationMetrics:
    expected_count = sum(case.trial_count for case in manifest.cases)
    if len(results) != expected_count:
        raise ValueError("Results must account for every configured trial")
    expected_by_position = {case.position: case for case in manifest.cases}
    seen = {(result.case_position, result.trial_index) for result in results}
    required = {
        (case.position, trial_index)
        for case in manifest.cases
        for trial_index in range(case.trial_count)
    }
    if seen != required:
        raise ValueError("Results must cover each trial exactly once")
    if any(
        result.expected_outcome != expected_by_position[result.case_position].expected_outcome
        for result in results
    ):
        raise ValueError("Result expectations must match the manifest")
    false_positives = sum(result.failure_kind == "false_positive" for result in results)
    false_negatives = sum(result.failure_kind == "false_negative" for result in results)
    operational = sum(result.failure_kind == "operational" for result in results)
    negative_trials = sum(
        case.trial_count for case in manifest.cases if case.expected_outcome == "rejected"
    )
    positive_trials = expected_count - negative_trials
    critical_false_positives = sum(
        result.failure_kind == "false_positive"
        and expected_by_position[result.case_position].critical
        for result in results
    )
    return EvaluationMetrics(
        result_count=len(results),
        false_positive_count=false_positives,
        false_negative_count=false_negatives,
        operational_failure_count=operational,
        critical_false_positive_count=critical_false_positives,
        false_positive_rate=_rate(false_positives, negative_trials),
        false_negative_rate=_rate(false_negatives, positive_trials),
    )


def decide_prompt_promotion(
    connection: Connection,
    *,
    baseline_run_id: Digest,
    candidate_run_id: Digest,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> PromptPromotionDecision:
    _require_autocommit(connection)
    existing = load_promotion_decision(connection, idempotency_key)
    if existing is not None:
        if (
            existing.baseline_run_id != baseline_run_id
            or existing.candidate_run_id != candidate_run_id
            or existing.actor != actor
        ):
            raise ValueError("Idempotency key belongs to a different promotion decision")
        return existing
    baseline = load_run(connection, baseline_run_id)
    candidate = load_run(connection, candidate_run_id)
    if baseline.manifest_id != candidate.manifest_id:
        raise ValueError("Baseline and candidate runs must use the same manifest")
    if baseline.prompt_release_id == candidate.prompt_release_id:
        raise ValueError("Baseline and candidate prompt releases must differ")
    manifest = load_manifest(connection, candidate.manifest_id)
    failures = _promotion_failures(manifest.policy, baseline.metrics, candidate.metrics)
    decision: Literal["approved", "rejected"] = "approved" if not failures else "rejected"
    reason = "Candidate clears every promotion check." if not failures else "; ".join(failures)
    promotion_id = _digest({"kind": "prompt_promotion", "idempotency_key": idempotency_key})
    promotion = PromptPromotionDecision(
        id=promotion_id,
        manifest_id=manifest.id,
        baseline_run_id=baseline.id,
        baseline_prompt_release_id=baseline.prompt_release_id,
        candidate_run_id=candidate.id,
        candidate_prompt_release_id=candidate.prompt_release_id,
        decision=decision,
        reason=reason,
        actor=actor,
        created_at=created_at,
    )
    with connection.transaction():
        _ = connection.execute(
            """
            INSERT INTO prompt_promotion_decisions (
              id, idempotency_key, manifest_id, baseline_run_id,
              baseline_prompt_release_id, candidate_run_id,
              candidate_prompt_release_id, decision, reason, baseline_metrics,
              candidate_metrics, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                promotion.id,
                idempotency_key,
                promotion.manifest_id,
                promotion.baseline_run_id,
                promotion.baseline_prompt_release_id,
                promotion.candidate_run_id,
                promotion.candidate_prompt_release_id,
                promotion.decision,
                promotion.reason,
                Jsonb(baseline.metrics.model_dump(mode="json")),
                Jsonb(candidate.metrics.model_dump(mode="json")),
                actor,
                created_at,
            ),
        )
        enqueue_projection(connection, "prompt_promotion", promotion.id, promotion, created_at)
    return promotion


def _load_current_cases(
    connection: Connection, policy: ManifestPolicy
) -> tuple[EvaluationManifestCase, ...]:
    rows = connection.execute(
        """
        WITH current_curations AS (
          SELECT DISTINCT ON (review_event_id) *
          FROM evaluation_case_curations
          ORDER BY review_event_id, created_at DESC, id DESC
        )
        SELECT c.id, c.review_event_id, c.expected_outcome, c.critical,
               s.title, s.company, s.raw_url, s.source, s.description, s.location,
               s.keywords, s.date_posted, s.observed_at, d.outcome,
               e.decision, e.target_profile
        FROM current_curations c
        JOIN review_events e ON e.id = c.review_event_id
        JOIN review_items i ON i.id = e.review_item_id
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
        WHERE c.action = 'include'
        ORDER BY c.review_event_id
        """
    ).fetchall()
    cases: list[EvaluationManifestCase] = []
    for position, row in enumerate(rows):
        critical = bool(row[3])
        cases.append(
            EvaluationManifestCase.model_validate(
                {
                    "position": position,
                    "curation_id": row[0],
                    "review_event_id": row[1],
                    "expected_outcome": row[2],
                    "critical": critical,
                    "trial_count": (
                        policy.critical_trial_count if critical else policy.regular_trial_count
                    ),
                    "input": {
                        "title": row[4],
                        "company": row[5],
                        "url": row[6],
                        "source": row[7],
                        "description": row[8],
                        "location": row[9],
                        "keywords": row[10],
                        "date_posted": row[11],
                        "observed_at": row[12],
                        "original_outcome": row[13],
                        "review_decision": row[14],
                        "target_profile": row[15],
                    },
                }
            )
        )
    return tuple(cases)


def _trial_result(
    run_id: Digest,
    case: EvaluationManifestCase,
    trial_index: int,
    result: EvaluationResult,
) -> EvaluationTrialResult:
    if isinstance(result, OperationalError):
        actual: ExpectedOutcome | None = None
    else:
        actual = "qualified" if isinstance(result, Qualified) else "rejected"
    failure = _failure_kind(case.expected_outcome, actual)
    result_id = _digest(
        {"run_id": run_id, "case_position": case.position, "trial_index": trial_index}
    )
    return EvaluationTrialResult(
        id=result_id,
        case_position=case.position,
        trial_index=trial_index,
        expected_outcome=case.expected_outcome,
        actual_outcome=actual,
        failure_kind=failure,
        reason=result.reason,
    )


def _failure_kind(
    expected: ExpectedOutcome, actual: ExpectedOutcome | None
) -> Literal["false_positive", "false_negative", "operational"] | None:
    if actual is None:
        return "operational"
    if actual == expected:
        return None
    return "false_positive" if actual == "qualified" else "false_negative"


def _promotion_failures(
    policy: ManifestPolicy,
    baseline: EvaluationMetrics,
    candidate: EvaluationMetrics,
) -> tuple[str, ...]:
    failures: list[str] = []
    if baseline.operational_failure_count:
        failures.append("Baseline has operational failures")
    if candidate.operational_failure_count:
        failures.append("Candidate has operational failures")
    if candidate.critical_false_positive_count:
        failures.append("Candidate qualified a critical expected-negative trial")
    if candidate.false_positive_rate > policy.max_false_positive_rate:
        failures.append("Candidate exceeds the false-positive threshold")
    if candidate.false_negative_rate > policy.max_false_negative_rate:
        failures.append("Candidate exceeds the false-negative threshold")
    if candidate.false_positive_rate > baseline.false_positive_rate:
        failures.append("Candidate regresses against baseline false positives")
    if candidate.false_negative_rate > baseline.false_negative_rate:
        failures.append("Candidate regresses against baseline false negatives")
    return tuple(failures)


def _insert_run(connection: Connection, run: EvaluationRun) -> None:
    metrics = run.metrics
    _ = connection.execute(
        """
        INSERT INTO evaluation_runs (
          id, idempotency_key, manifest_id, prompt_release_id,
          expected_result_count, result_count, false_positive_count,
          false_negative_count, operational_failure_count,
          critical_false_positive_count, false_positive_rate,
          false_negative_rate, implementation_ref, completed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run.id,
            run.idempotency_key,
            run.manifest_id,
            run.prompt_release_id,
            metrics.result_count,
            metrics.result_count,
            metrics.false_positive_count,
            metrics.false_negative_count,
            metrics.operational_failure_count,
            metrics.critical_false_positive_count,
            metrics.false_positive_rate,
            metrics.false_negative_rate,
            run.implementation_ref,
            run.completed_at,
        ),
    )
    for result in run.results:
        _ = connection.execute(
            """
            INSERT INTO evaluation_case_results (
              id, run_id, manifest_id, prompt_release_id, case_position,
              trial_index, expected_outcome, actual_outcome, failure_kind, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                result.id,
                run.id,
                run.manifest_id,
                run.prompt_release_id,
                result.case_position,
                result.trial_index,
                result.expected_outcome,
                result.actual_outcome,
                result.failure_kind,
                result.reason,
            ),
        )


def load_run_by_key(connection: Connection, idempotency_key: str) -> EvaluationRun | None:
    row = connection.execute(
        "SELECT id FROM evaluation_runs WHERE idempotency_key = %s", (idempotency_key,)
    ).fetchone()
    return None if row is None else load_run(connection, str(row[0]))


def load_run(connection: Connection, run_id: Digest) -> EvaluationRun:
    row = connection.execute(
        """
        SELECT idempotency_key, manifest_id, prompt_release_id, result_count,
               false_positive_count, false_negative_count, operational_failure_count,
               critical_false_positive_count, false_positive_rate,
               false_negative_rate, implementation_ref, completed_at
        FROM evaluation_runs WHERE id = %s
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Evaluation run does not exist")
    result_rows = connection.execute(
        """
        SELECT id, case_position, trial_index, expected_outcome, actual_outcome,
               failure_kind, reason
        FROM evaluation_case_results WHERE run_id = %s
        ORDER BY case_position, trial_index
        """,
        (run_id,),
    ).fetchall()
    return EvaluationRun(
        id=run_id,
        idempotency_key=str(row[0]),
        manifest_id=str(row[1]),
        prompt_release_id=str(row[2]),
        metrics=EvaluationMetrics(
            result_count=int(str(row[3])),
            false_positive_count=int(str(row[4])),
            false_negative_count=int(str(row[5])),
            operational_failure_count=int(str(row[6])),
            critical_false_positive_count=int(str(row[7])),
            false_positive_rate=Decimal(str(row[8])),
            false_negative_rate=Decimal(str(row[9])),
        ),
        implementation_ref=str(row[10]),
        results=tuple(
            EvaluationTrialResult.model_validate(
                {
                    "id": result[0],
                    "case_position": result[1],
                    "trial_index": result[2],
                    "expected_outcome": result[3],
                    "actual_outcome": result[4],
                    "failure_kind": result[5],
                    "reason": result[6],
                }
            )
            for result in result_rows
        ),
        completed_at=datetime.fromisoformat(str(row[11])),
    )


def load_promotion_decision(
    connection: Connection, idempotency_key: str
) -> PromptPromotionDecision | None:
    row = connection.execute(
        """
        SELECT id, manifest_id, baseline_run_id, baseline_prompt_release_id,
               candidate_run_id, candidate_prompt_release_id, decision, reason,
               actor, created_at
        FROM prompt_promotion_decisions WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return PromptPromotionDecision.model_validate(
        {
            "id": row[0],
            "manifest_id": row[1],
            "baseline_run_id": row[2],
            "baseline_prompt_release_id": row[3],
            "candidate_run_id": row[4],
            "candidate_prompt_release_id": row[5],
            "decision": row[6],
            "reason": row[7],
            "actor": row[8],
            "created_at": row[9],
        }
    )


def _insert_curation(
    connection: Connection, idempotency_key: str, curation: CuratedReviewEvent
) -> None:
    _ = connection.execute(
        """
        INSERT INTO evaluation_case_curations (
          id, idempotency_key, review_event_id, action, expected_outcome,
          critical, reason, actor, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            curation.id,
            idempotency_key,
            curation.review_event_id,
            curation.action,
            curation.expected_outcome,
            curation.critical,
            curation.reason,
            curation.actor,
            curation.created_at,
        ),
    )


def _load_curation_by_key(
    connection: Connection, idempotency_key: str
) -> CuratedReviewEvent | None:
    row = connection.execute(
        """
        SELECT id, review_event_id, action, expected_outcome, critical,
               reason, actor, created_at
        FROM evaluation_case_curations WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return CuratedReviewEvent.model_validate(
        {
            "id": row[0],
            "review_event_id": row[1],
            "action": row[2],
            "expected_outcome": row[3],
            "critical": row[4],
            "reason": row[5],
            "actor": row[6],
            "created_at": row[7],
        }
    )


def _require_matching_curation(
    existing: CuratedReviewEvent,
    review_event_id: UUID,
    action: Literal["include", "exclude"],
    critical: bool,
    reason: str,
    actor: str,
) -> None:
    if (
        existing.review_event_id != review_event_id
        or existing.action != action
        or existing.critical != critical
        or existing.reason != reason
        or existing.actor != actor
    ):
        raise ValueError("Idempotency key belongs to a different curation command")


def enqueue_projection(
    connection: Connection,
    kind: str,
    source_id: str,
    payload: BaseModel,
    created_at: datetime,
) -> None:
    data = payload.model_dump(mode="json")
    payload_digest = _digest(data)
    projection_id = _digest({"kind": kind, "source_id": source_id})
    _ = connection.execute(
        """
        INSERT INTO langfuse_projection_items (
          id, kind, source_id, payload_digest, payload, state, created_at
        ) VALUES (%s, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (kind, source_id) DO NOTHING
        """,
        (projection_id, kind, source_id, payload_digest, Jsonb(data), created_at),
    )


def _rate(count: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return (Decimal(count) / Decimal(denominator)).quantize(Decimal("0.0000001"))


def _digest(value: object) -> Digest:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Evaluation manifest operations require an autocommit connection")
