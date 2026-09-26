# pyright: reportUnusedFunction=false
from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from job_finder.benchmarks.qualification_evidence import (
    ExperimentInputId,
    FixtureSetId,
    Phase,
    PhaseFixtureSet,
    QualificationEvidence,
    QualificationEvidenceId,
    RelevanceExperimentInput,
    fixture_set_id,
    experiment_input_id,
    qualification_evidence_id,
    record_relevance_comparison,
    store_fixture_set,
    store_relevance_experiment_input,
)
from job_finder.benchmarks.qualification_execution import (
    QualificationEvidenceExecution,
    execute_qualification_evidence,
)
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.mcp_tools.common import APPEND_ONLY, READ_ONLY, McpDependencies

_DIGEST = r"^[0-9a-f]{64}$"
_TargetId = Annotated[QualificationTargetId, Field(pattern=_DIGEST)]
_EvidenceId = Annotated[QualificationEvidenceId, Field(pattern=_DIGEST)]
_FixtureId = Annotated[FixtureSetId, Field(pattern=_DIGEST)]
_ExperimentId = Annotated[ExperimentInputId, Field(pattern=_DIGEST)]


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class QualificationEvidenceSummary(_Model):
    id: QualificationEvidenceId
    target_id: QualificationTargetId
    phase: Phase
    origin: str
    outcome: str
    created_at: datetime


class QualificationEvidencePage(_Model):
    items: tuple[QualificationEvidenceSummary, ...] = Field(max_length=100)


def register_qualification_evidence_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    _register_execution_tool(mcp, dependencies)
    _register_evidence_reads(mcp, dependencies)
    _register_fixture_tools(mcp, dependencies)
    _register_relevance_tools(mcp, dependencies)


def _register_execution_tool(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_evidence_execute(
        idempotency_key: Annotated[str, Field(min_length=1)],
        target_id: _TargetId,
        phase: Phase,
        input_id: Annotated[str, Field(pattern=_DIGEST)],
    ) -> QualificationEvidenceExecution:
        """Execute frozen qualification input once against the verified build artifact."""
        if dependencies.implementation_artifact_path is None:
            raise ToolError("Executing build artifact is not configured")
        if dependencies.resolve_provider_credentials is None:
            raise ToolError("Provider credential resolver is not configured")
        try:
            with dependencies.connect() as connection:
                return execute_qualification_evidence(
                    connection,
                    idempotency_key=idempotency_key,
                    target_id=target_id,
                    phase=phase,
                    input_id=input_id,
                    artifact_path=dependencies.implementation_artifact_path,
                    resolve_credentials=dependencies.resolve_provider_credentials,
                    completed_at=dependencies.now(),
                    created_by=dependencies.actor,
                )
        except ValueError as error:
            raise ToolError(str(error)) from error


def _register_evidence_reads(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def qualification_evidence_list(
        target_id: _TargetId,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> QualificationEvidencePage:
        """List recent evidence for one qualification target, including failed and synthetic runs."""
        with dependencies.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, target_id, phase, origin, outcome, created_at
                FROM qualification_phase_evidence WHERE target_id = %s
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                (target_id, limit),
            ).fetchall()
        return QualificationEvidencePage(
            items=tuple(
                QualificationEvidenceSummary.model_validate(
                    dict(
                        zip(
                            ("id", "target_id", "phase", "origin", "outcome", "created_at"),
                            row,
                            strict=True,
                        )
                    )
                )
                for row in rows
            )
        )

    @mcp.tool(annotations=READ_ONLY)
    def qualification_evidence_get(evidence_id: _EvidenceId) -> QualificationEvidence:
        """Inspect one immutable phase evidence record and verify its content identity."""
        with dependencies.connect() as connection:
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s", (evidence_id,)
            ).fetchone()
        if row is None:
            raise ToolError("Qualification evidence does not exist")
        evidence = QualificationEvidence.model_validate(row[0])
        if qualification_evidence_id(evidence) != evidence_id:
            raise ToolError("Qualification evidence identity is invalid")
        return evidence


def _register_fixture_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_fixture_set_store(content: PhaseFixtureSet) -> FixtureSetId:
        """Freeze a curated phase fixture set for canonical benchmark execution."""
        with dependencies.connect() as connection:
            return store_fixture_set(
                connection, content, created_at=dependencies.now(), created_by=dependencies.actor
            )

    @mcp.tool(annotations=READ_ONLY)
    def qualification_fixture_set_get(fixture_id: _FixtureId) -> PhaseFixtureSet:
        """Inspect one immutable fixture set and verify its content identity."""
        with dependencies.connect() as connection:
            row = connection.execute(
                "SELECT content FROM qualification_fixture_sets WHERE id = %s", (fixture_id,)
            ).fetchone()
        if row is None:
            raise ToolError("Qualification fixture set does not exist")
        content = PhaseFixtureSet.model_validate(row[0])
        if fixture_set_id(content) != fixture_id:
            raise ToolError("Qualification fixture set identity is invalid")
        return content


def _register_relevance_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_relevance_input_store(content: RelevanceExperimentInput) -> ExperimentInputId:
        """Freeze the manifest, rates, provider settings, and input path for relevance evaluation."""
        with dependencies.connect() as connection:
            return store_relevance_experiment_input(
                connection, content, created_at=dependencies.now(), created_by=dependencies.actor
            )

    @mcp.tool(annotations=READ_ONLY)
    def qualification_relevance_input_get(input_id: _ExperimentId) -> RelevanceExperimentInput:
        """Inspect one frozen relevance experiment input and verify its identity."""
        with dependencies.connect() as connection:
            row = connection.execute(
                "SELECT content FROM relevance_experiment_inputs WHERE id = %s", (input_id,)
            ).fetchone()
        if row is None:
            raise ToolError("Relevance experiment input does not exist")
        content = RelevanceExperimentInput.model_validate(row[0])
        if experiment_input_id(content) != input_id:
            raise ToolError("Relevance experiment input identity is invalid")
        return content

    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_relevance_comparison_create(
        baseline_evidence_id: _EvidenceId, candidate_evidence_id: _EvidenceId
    ) -> str:
        """Record a comparison of two canonical relevance runs on the same frozen input."""
        with dependencies.connect() as connection:
            rows = connection.execute(
                "SELECT id, content FROM qualification_phase_evidence WHERE id IN (%s, %s)",
                (baseline_evidence_id, candidate_evidence_id),
            ).fetchall()
            found = {str(row[0]): QualificationEvidence.model_validate(row[1]) for row in rows}
            if str(baseline_evidence_id) not in found or str(candidate_evidence_id) not in found:
                raise ToolError("Relevance evidence does not exist")
            baseline = found[str(baseline_evidence_id)]
            candidate = found[str(candidate_evidence_id)]
            if (
                qualification_evidence_id(baseline) != baseline_evidence_id
                or qualification_evidence_id(candidate) != candidate_evidence_id
            ):
                raise ToolError("Relevance evidence identity is invalid")
            if baseline.origin != "canonical" or candidate.origin != "canonical":
                raise ToolError("Relevance comparison requires canonical evidence")
            return record_relevance_comparison(
                connection,
                baseline,
                candidate,
                created_at=dependencies.now(),
                created_by=dependencies.actor,
            )
