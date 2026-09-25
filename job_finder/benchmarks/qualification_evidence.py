from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, ClassVar, Literal, NewType, Self

import psycopg
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from psycopg.types.json import Jsonb

from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.implementation_artifacts import ImplementationArtifactId
from job_finder.evaluation.qualification_components import (
    ComponentReleaseId,
    QualificationTargetId,
)

ExperimentInputId = NewType("ExperimentInputId", str)
FixtureSetId = NewType("FixtureSetId", str)
QualificationEvidenceId = NewType("QualificationEvidenceId", str)
_DIGEST = r"^[0-9a-f]{64}$"
Phase = Literal["input_preparation", "relevance", "enrichment", "deduplication", "composition"]


class EvidenceModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ProviderExperimentSettings(EvidenceModel):
    provider: Literal["openrouter", "typesafe"]
    temperature: float = Field(ge=0, le=2)
    seed: int | None = None
    retry_limit: int = Field(ge=0, le=10)


class RelevanceExperimentInput(EvidenceModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_DIGEST)
    exchange_rates: ExchangeRateSnapshot
    provider_settings: ProviderExperimentSettings
    input_path: Literal["direct", "ats"]


class FixtureCase(EvidenceModel):
    input: dict[str, JsonValue]
    expected: dict[str, JsonValue]
    input_path: Literal["direct", "ats"]


class PhaseFixtureSet(EvidenceModel):
    schema_version: Literal[1] = 1
    phase: Literal["input_preparation", "enrichment", "deduplication", "composition"]
    cases: tuple[FixtureCase, ...] = Field(min_length=1)


class ProviderAttemptEvidence(EvidenceModel):
    input_digest: str = Field(pattern=_DIGEST)
    requested_model: str = Field(min_length=1)
    observed_model: str | None = None
    provider_response_id: str | None = None
    status: Literal["accepted", "retryable_error", "terminal_error"]
    response: JsonValue | None = None
    observed_at: datetime


class QualificationEvidence(EvidenceModel):
    schema_version: Literal[1] = 1
    target_id: Annotated[QualificationTargetId, Field(pattern=_DIGEST)]
    phase: Phase
    component_release_id: Annotated[ComponentReleaseId, Field(pattern=_DIGEST)] | None = None
    experiment_input_id: Annotated[ExperimentInputId, Field(pattern=_DIGEST)] | None = None
    fixture_set_id: Annotated[FixtureSetId, Field(pattern=_DIGEST)] | None = None
    executor_artifact_id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    origin: Literal["canonical", "synthetic", "imported"]
    outcome: Literal["passed", "failed"]
    result: dict[str, JsonValue]
    attempts: tuple[ProviderAttemptEvidence, ...] = ()
    completed_at: datetime

    @model_validator(mode="after")
    def phase_has_one_input_and_its_component(self) -> Self:
        if self.phase == "relevance":
            if self.experiment_input_id is None or self.fixture_set_id is not None:
                raise ValueError("Relevance evidence requires one frozen experiment input")
        elif self.fixture_set_id is None or self.experiment_input_id is not None:
            raise ValueError("Phase evidence requires one frozen fixture set")
        if (self.phase == "composition") != (self.component_release_id is None):
            raise ValueError("Only composition evidence omits a component release")
        return self


def experiment_input_id(content: RelevanceExperimentInput) -> ExperimentInputId:
    return ExperimentInputId(_content_digest(content))


def fixture_set_id(content: PhaseFixtureSet) -> FixtureSetId:
    return FixtureSetId(_content_digest(content))


def qualification_evidence_id(content: QualificationEvidence) -> QualificationEvidenceId:
    return QualificationEvidenceId(_content_digest(content))


def require_comparable_relevance_evidence(
    baseline: QualificationEvidence,
    candidate: QualificationEvidence,
) -> ExperimentInputId:
    if baseline.phase != "relevance" or candidate.phase != "relevance":
        raise ValueError("Relevance comparison requires two relevance evidence records")
    if (
        baseline.experiment_input_id is None
        or baseline.experiment_input_id != candidate.experiment_input_id
    ):
        raise ValueError("Relevance comparison requires the same frozen experiment input")
    return baseline.experiment_input_id


def _content_digest(content: EvidenceModel) -> str:
    encoded = json.dumps(
        content.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def store_relevance_experiment_input(
    connection: psycopg.Connection[tuple[object, ...]],
    content: RelevanceExperimentInput,
    *,
    created_at: datetime,
    created_by: str,
) -> ExperimentInputId:
    identity = experiment_input_id(content)
    _ = connection.execute(
        """
        INSERT INTO relevance_experiment_inputs (
          id, manifest_id, content, created_at, created_by
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            identity,
            content.manifest_id,
            Jsonb(content.model_dump(mode="json")),
            created_at,
            created_by,
        ),
    )
    row = connection.execute(
        "SELECT content FROM relevance_experiment_inputs WHERE id = %s", (identity,)
    ).fetchone()
    if row is None or RelevanceExperimentInput.model_validate(row[0]) != content:
        raise ValueError("Stored relevance experiment differs from its identity")
    return identity


def store_fixture_set(
    connection: psycopg.Connection[tuple[object, ...]],
    content: PhaseFixtureSet,
    *,
    created_at: datetime,
    created_by: str,
) -> FixtureSetId:
    identity = fixture_set_id(content)
    _ = connection.execute(
        """
        INSERT INTO qualification_fixture_sets (id, phase, content, created_at, created_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (identity, content.phase, Jsonb(content.model_dump(mode="json")), created_at, created_by),
    )
    row = connection.execute(
        "SELECT content FROM qualification_fixture_sets WHERE id = %s", (identity,)
    ).fetchone()
    if row is None or PhaseFixtureSet.model_validate(row[0]) != content:
        raise ValueError("Stored fixture set differs from its identity")
    return identity


def store_qualification_evidence(
    connection: psycopg.Connection[tuple[object, ...]],
    content: QualificationEvidence,
    *,
    created_at: datetime,
    created_by: str,
) -> QualificationEvidenceId:
    identity = qualification_evidence_id(content)
    _ = connection.execute(
        """
        INSERT INTO qualification_phase_evidence (
          id, target_id, phase, component_release_id, experiment_input_id,
          fixture_set_id, executor_artifact_id, origin, outcome,
          content, created_at, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            identity,
            content.target_id,
            content.phase,
            content.component_release_id,
            content.experiment_input_id,
            content.fixture_set_id,
            content.executor_artifact_id,
            content.origin,
            content.outcome,
            Jsonb(content.model_dump(mode="json")),
            created_at,
            created_by,
        ),
    )
    row = connection.execute(
        "SELECT content FROM qualification_phase_evidence WHERE id = %s", (identity,)
    ).fetchone()
    if row is None or QualificationEvidence.model_validate(row[0]) != content:
        raise ValueError("Stored qualification evidence differs from its identity")
    return identity


def record_relevance_comparison(
    connection: psycopg.Connection[tuple[object, ...]],
    baseline: QualificationEvidence,
    candidate: QualificationEvidence,
    *,
    created_at: datetime,
    created_by: str,
) -> str:
    experiment_id = require_comparable_relevance_evidence(baseline, candidate)
    baseline_id = qualification_evidence_id(baseline)
    candidate_id = qualification_evidence_id(candidate)
    if baseline_id == candidate_id:
        raise ValueError("Relevance comparison requires distinct evidence records")
    comparison = {
        "experiment_input_id": experiment_id,
        "baseline_evidence_id": baseline_id,
        "candidate_evidence_id": candidate_id,
    }
    identity = hashlib.sha256(
        json.dumps(comparison, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _ = connection.execute(
        """
        INSERT INTO qualification_relevance_comparisons (
          id, experiment_input_id, baseline_evidence_id,
          candidate_evidence_id, created_at, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (identity, experiment_id, baseline_id, candidate_id, created_at, created_by),
    )
    return identity
