from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg
import pytest
from fastmcp import Client
from psycopg import sql
from pydantic import JsonValue

from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.ats.models import AtsAvailable, AtsNotApplicable
from job_finder.benchmarks.qualification_evidence import (
    FixtureCase,
    PhaseFixtureSet,
    QualificationEvidence,
    store_fixture_set,
)
from job_finder.benchmarks.qualification_execution import execute_qualification_evidence
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import (
    qualification_target_id,
)
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)
from job_finder.mcp_server import McpDependencies, create_mcp_server

_NOW = datetime(2026, 9, 26, tzinfo=UTC)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_evidence_execution_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def _passing_input_preparation_fixture() -> PhaseFixtureSet:
    markdown = "Title: Backend engineer\nBuild services."
    url = "https://jobs.lever.co/acme/role-1"
    direct_evidence = AtsNotApplicable().model_dump(mode="json")
    ats_evidence = AtsAvailable(
        source="lever",
        location="Berlin",
        locations=("Berlin",),
        workplace_type="OnSite",
        country="DE",
        description="A detailed ATS description",
    ).model_dump(mode="json")
    listing: dict[str, JsonValue] = {
        "title": "Backend engineer",
        "company": "acme",
        "url": url,
        "source": "lever",
        "keywords_matched": ["backend"],
        "date_posted": None,
        "date_scraped": "2026-09-25",
        "description": markdown,
        "location": "",
        "profile": "",
    }
    ats_description = (
        "## ATS Structured Data (from lever API)\n"
        "- Primary location: Berlin\n"
        "- All listed locations: Berlin\n"
        "- Workplace type: OnSite\n"
        "- Country fallback when locations are non-geographic: DE\n"
        "---\n\nA detailed ATS description"
    )
    return PhaseFixtureSet(
        phase="input_preparation",
        cases=(
            FixtureCase(
                input={
                    "markdown": markdown,
                    "url": url,
                    "keyword": "backend",
                    "scraped_on": "2026-09-25",
                    "ats_evidence": direct_evidence,
                },
                expected={
                    "listing": listing,
                    "ats_evidence": direct_evidence,
                    "body": markdown,
                    "structural_decision": {"kind": "pass"},
                },
                input_path="direct",
            ),
            FixtureCase(
                input={
                    "markdown": markdown,
                    "url": url,
                    "keyword": "backend",
                    "scraped_on": "2026-09-25",
                    "ats_evidence": ats_evidence,
                },
                expected={
                    "listing": {**listing, "description": ats_description, "location": "Berlin"},
                    "ats_evidence": ats_evidence,
                    "body": "A detailed ATS description",
                    "structural_decision": {
                        "kind": "rejected",
                        "reason": "ATS workplaceType=OnSite (Berlin)",
                    },
                },
                input_path="ats",
            ),
        ),
    )


def test_execution_ledger_returns_result_and_rejects_changed_request(
    authority_schema: str,
) -> None:
    settings = PostgresContractSettings.from_environment()
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
            )
            apply_migrations(connection)
            artifact, _, target = _store_default_qualification_target(connection, _NOW)
            target_id = qualification_target_id(target)
            assert write_implementation_artifact(root, artifact_path) == artifact
            fixture_id = store_fixture_set(
                connection,
                _passing_input_preparation_fixture(),
                created_at=_NOW,
                created_by="contract",
            )
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=_NOW, created_by="contract"
            )

            def credentials(_connection: object):
                raise AssertionError("Input preparation does not use provider credentials")

            def execute(input_id: str):
                return execute_qualification_evidence(
                    connection,
                    idempotency_key="contract:once",
                    target_id=target_id,
                    phase="input_preparation",
                    input_id=input_id,
                    artifact_path=artifact_path,
                    resolve_credentials=credentials,
                    completed_at=_NOW,
                    created_by="contract",
                )

            first = execute(fixture_id)
            assert first.state == "completed"
            assert first.evidence_id is not None
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s",
                (first.evidence_id,),
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.origin == "canonical"
            assert evidence.outcome == "passed"
            assert evidence.phase == "input_preparation"
            second = execute(fixture_id)
            assert second.state == "completed"
            assert second.evidence_id == first.evidence_id
            assert connection.execute(
                "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                (target_id,),
            ).fetchone() == (1,)
            with pytest.raises(ValueError, match="different evidence request"):
                execute("a" * 64)
            assert connection.execute(
                "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                (target_id,),
            ).fetchone() == (1,)

            @contextmanager
            def connect() -> Generator[psycopg.Connection[tuple[object, ...]]]:
                with psycopg.connect(settings.postgres_dsn, autocommit=True) as tool_connection:
                    tool_connection.execute(
                        sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
                    )
                    yield tool_connection

            server = create_mcp_server(
                McpDependencies(
                    connect=connect,
                    actor="contract",
                    now=lambda: _NOW,
                    implementation_artifact_path=artifact_path,
                    resolve_provider_credentials=credentials,
                )
            )

            async def retry_through_mcp() -> None:
                async with Client(server) as client:
                    result = await client.call_tool(
                        "qualification_evidence_execute",
                        {
                            "idempotency_key": "contract:once",
                            "target_id": target_id,
                            "phase": "input_preparation",
                            "input_id": fixture_id,
                        },
                    )
                    assert result.structured_content is not None
                    assert result.structured_content["state"] == "completed"
                    assert result.structured_content["evidence_id"] == first.evidence_id

            asyncio.run(retry_through_mcp())
            assert connection.execute(
                "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                (target_id,),
            ).fetchone() == (1,)


def test_interrupted_execution_is_failed_without_replaying_calls(
    authority_schema: str,
) -> None:
    settings = PostgresContractSettings.from_environment()
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
            )
            apply_migrations(connection)
            artifact, _, target = _store_default_qualification_target(connection, _NOW)
            target_id = qualification_target_id(target)
            assert write_implementation_artifact(root, artifact_path) == artifact
            fixture_id = store_fixture_set(
                connection,
                PhaseFixtureSet(
                    phase="input_preparation",
                    cases=(FixtureCase(input={}, expected={}, input_path="direct"),),
                ),
                created_at=_NOW,
                created_by="contract",
            )
            connection.execute(
                """
                INSERT INTO qualification_evidence_executions (
                  idempotency_key, target_id, phase, input_id, artifact_id, state, created_at
                ) VALUES (%s, %s, 'input_preparation', %s, %s, 'running', %s)
                """,
                ("contract:interrupted", target_id, fixture_id, artifact.id, _NOW),
            )

            def credentials(_connection: object):
                raise AssertionError("A retry must not resolve provider credentials")

            result = execute_qualification_evidence(
                connection,
                idempotency_key="contract:interrupted",
                target_id=target_id,
                phase="input_preparation",
                input_id=fixture_id,
                artifact_path=artifact_path,
                resolve_credentials=credentials,
                completed_at=_NOW,
                created_by="contract",
            )
            assert result.state == "failed"
            assert result.failure == "interrupted_execution"
            assert result.evidence_id is None
            assert connection.execute(
                "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                (target_id,),
            ).fetchone() == (0,)


def test_failed_execution_is_recorded_and_replayed_identically(
    authority_schema: str,
) -> None:
    settings = PostgresContractSettings.from_environment()
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
            )
            apply_migrations(connection)
            artifact, _, target = _store_default_qualification_target(connection, _NOW)
            target_id = qualification_target_id(target)
            assert write_implementation_artifact(root, artifact_path) == artifact
            fixture_id = store_fixture_set(
                connection,
                PhaseFixtureSet(
                    phase="enrichment",
                    cases=(FixtureCase(input={}, expected={}, input_path="direct"),),
                ),
                created_at=_NOW,
                created_by="contract",
            )
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=_NOW, created_by="contract"
            )

            def credentials(_connection: object):
                raise AssertionError("Input preparation does not use provider credentials")

            def execute(idempotency_key: str):
                return execute_qualification_evidence(
                    connection,
                    idempotency_key=idempotency_key,
                    target_id=target_id,
                    phase="input_preparation",
                    input_id=fixture_id,
                    artifact_path=artifact_path,
                    resolve_credentials=credentials,
                    completed_at=_NOW,
                    created_by="contract",
                )

            fresh = execute("contract:new-attempt")
            assert fresh.state == "failed"
            assert fresh.failure == "execution_failed"
            assert fresh.evidence_id is None
            assert connection.execute(
                "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                (target_id,),
            ).fetchone() == (0,)
            retry = execute("contract:new-attempt")
            assert retry == fresh
            assert connection.execute(
                "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                (target_id,),
            ).fetchone() == (0,)
