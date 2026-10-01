from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict, JsonValue

from job_finder.ats.models import AtsAvailable, AtsEvidence, AtsNotApplicable, AtsUnavailable
from job_finder.ats.policy import ats_structural_filter
from job_finder.benchmarks.qualification_evidence import (
    FixtureSetId,
    QualificationEvidence,
    QualificationEvidenceId,
    load_fixture_set,
    store_qualification_evidence,
    summarize_fixture_results,
)
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.evaluation.qualification_prompt_compilations import (
    load_compiled_qualification_target,
)
from job_finder.jobs.listings import JobListing
from job_finder.jobs.scraping import parse_job_details
from job_finder.jobs.structural_filter import (
    StructuralDecision,
    StructuralRejection,
    structural_filter,
)
from job_finder.pipeline.orchestration import prepare_listing_with_ats


class InputPreparationFixtureInput(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    markdown: str
    url: str
    keyword: str
    scraped_on: date
    page_title: str = ""
    ats_evidence: AtsEvidence


class InputPreparationObservation(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    listing: JobListing
    ats_evidence: JsonValue
    body: str
    structural_decision: StructuralDecision


def prepare_fixture_input(
    content: InputPreparationFixtureInput,
) -> InputPreparationObservation:
    listing = parse_job_details(
        content.markdown,
        content.url,
        content.keyword,
        scraped_on=content.scraped_on,
        page_title=content.page_title,
    )
    listing, evidence_json, body = prepare_listing_with_ats(listing, content.ats_evidence)
    decision = ats_structural_filter(content.ats_evidence)
    if not isinstance(decision, StructuralRejection):
        decision = structural_filter(listing)
    return InputPreparationObservation(
        listing=listing,
        ats_evidence=evidence_json,
        body=body,
        structural_decision=decision,
    )


def execute_input_preparation_fixture_set(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    fixture_id: FixtureSetId,
    artifact_path: Path,
    *,
    completed_at: datetime,
    created_by: str,
) -> QualificationEvidenceId:
    target = load_compiled_qualification_target(connection, target_id, artifact_path).target
    fixtures = load_fixture_set(connection, fixture_id, phase="input_preparation")
    results: list[dict[str, JsonValue]] = []
    for case in fixtures.cases:
        content = InputPreparationFixtureInput.model_validate(case.input)
        if (case.input_path == "direct") != isinstance(content.ats_evidence, AtsNotApplicable):
            raise ValueError("Fixture input path differs from ATS evidence")
        if isinstance(content.ats_evidence, AtsAvailable | AtsUnavailable) and (
            content.ats_evidence.source not in target.input_preparation.ats_sources
        ):
            raise ValueError("Fixture ATS source is not supported by target")
        expected = InputPreparationObservation.model_validate(case.expected)
        observed = prepare_fixture_input(content)
        passed = observed == expected
        results.append(
            {
                "input_path": case.input_path,
                "passed": passed,
                "observed": observed.model_dump(mode="json"),
                "expected": expected.model_dump(mode="json"),
            }
        )
    outcome, result = summarize_fixture_results(results)
    evidence = QualificationEvidence(
        target_id=target_id,
        phase="input_preparation",
        component_release_id=target.content.input_preparation_release_id,
        fixture_set_id=fixture_id,
        executor_artifact_id=target.content.artifact_id,
        origin="canonical",
        outcome=outcome,
        result=result,
        completed_at=completed_at,
    )
    return store_qualification_evidence(
        connection, evidence, created_at=completed_at, created_by=created_by
    )
