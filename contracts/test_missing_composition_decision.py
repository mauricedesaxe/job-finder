from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.benchmarks.qualification_promotions import (
    PromotionEvidenceSelection,
    QualificationPromotionDecision,
    preview_qualification_promotion,
    record_qualification_promotion_decision,
)
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import (
    build_qualification_target,
    qualification_target_id,
    store_component_release,
    store_qualification_target,
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_missing_composition_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_rejected_decision_records_missing_composition(authority_schema: str) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    root = Path(__file__).resolve().parents[1]
    settings = PostgresContractSettings.from_environment()
    with NamedTemporaryFile(dir=root, prefix=".missing-composition-", suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            _ = connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
            )
            _ = apply_migrations(connection)
            artifact, components, baseline = _store_default_qualification_target(connection, now)
            assert write_implementation_artifact(root, artifact_path) == artifact
            alternate_input = components[0].model_copy(
                update={"ats_sources": components[0].ats_sources[:-1]}
            )
            _ = store_component_release(
                connection, alternate_input, created_at=now, created_by="owner"
            )
            candidate = build_qualification_target(alternate_input, *components[1:])
            candidate_id = store_qualification_target(
                connection, candidate, created_at=now, created_by="owner"
            )
            baseline_id = qualification_target_id(baseline)
            evidence = PromotionEvidenceSelection()
            preview = preview_qualification_promotion(
                connection, baseline_id, candidate_id, evidence, artifact_path
            )
            assert not preview.eligible
            assert "Composition evidence is missing" in preview.failures

            def record(
                decision: Literal["approved", "rejected"], key: str
            ) -> QualificationPromotionDecision:
                return record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline_id,
                    candidate_target_id=candidate_id,
                    evidence=evidence,
                    artifact_path=artifact_path,
                    decision=decision,
                    reason="Evidence missing",
                    actor="owner",
                    created_at=now,
                    idempotency_key=key,
                )

            with pytest.raises(ValueError, match="Ineligible qualification target"):
                _ = record("approved", "missing-approved")
            rejected = record("rejected", "missing-composition-decision")
            assert rejected.evidence.composition_evidence_id is None
            assert record("rejected", "missing-composition-decision") == rejected
