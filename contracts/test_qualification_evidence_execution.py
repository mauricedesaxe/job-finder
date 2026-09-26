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

import job_finder.benchmarks.qualification_execution as execution_module
from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.benchmarks.qualification_evidence import (
    FixtureCase,
    PhaseFixtureSet,
    QualificationEvidence,
    store_fixture_set,
    store_qualification_evidence,
)
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import qualification_target_id
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


def test_execution_ledger_returns_result_and_rejects_changed_request(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
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
            evidence_id = store_qualification_evidence(
                connection,
                QualificationEvidence(
                    target_id=target_id,
                    phase="input_preparation",
                    component_release_id=target.input_preparation_release_id,
                    fixture_set_id=fixture_id,
                    executor_artifact_id=artifact.id,
                    origin="canonical",
                    outcome="passed",
                    result={},
                    completed_at=_NOW,
                ),
                created_at=_NOW,
                created_by="contract",
            )
            calls = 0

            def run_once(*_args: object, **_kwargs: object):
                nonlocal calls
                calls += 1
                return evidence_id

            monkeypatch.setattr(execution_module, "_run_phase", run_once)

            def credentials(_connection: object):
                raise AssertionError("Input preparation does not use provider credentials")

            def execute(input_id: str):
                return execution_module.execute_qualification_evidence(
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
            second = execute(fixture_id)
            assert first.state == second.state == "completed"
            assert first.evidence_id == second.evidence_id == evidence_id
            assert calls == 1
            with pytest.raises(ValueError, match="different evidence request"):
                execute("a" * 64)
            assert calls == 1

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
                    assert result.structured_content["evidence_id"] == evidence_id

            asyncio.run(retry_through_mcp())
            assert calls == 1


def test_interrupted_execution_is_failed_without_replaying_calls(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
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

            def replay(*_args: object, **_kwargs: object):
                raise AssertionError("A retry must not replay provider calls")

            monkeypatch.setattr(execution_module, "_run_phase", replay)
            result = execution_module.execute_qualification_evidence(
                connection,
                idempotency_key="contract:interrupted",
                target_id=target_id,
                phase="input_preparation",
                input_id=fixture_id,
                artifact_path=artifact_path,
                resolve_credentials=replay,
                completed_at=_NOW,
                created_by="contract",
            )
            assert result.state == "failed"
            assert result.failure == "interrupted_execution"
            assert result.evidence_id is None
            calls = 0

            def new_attempt(*_args: object, **_kwargs: object):
                nonlocal calls
                calls += 1
                raise ValueError("Fixture is invalid")

            monkeypatch.setattr(execution_module, "_run_phase", new_attempt)
            fresh = execution_module.execute_qualification_evidence(
                connection,
                idempotency_key="contract:new-attempt",
                target_id=target_id,
                phase="input_preparation",
                input_id=fixture_id,
                artifact_path=artifact_path,
                resolve_credentials=replay,
                completed_at=_NOW,
                created_by="contract",
            )
            assert fresh.state == "failed"
            assert fresh.failure == "execution_failed"
            assert calls == 1
            retry = execution_module.execute_qualification_evidence(
                connection,
                idempotency_key="contract:new-attempt",
                target_id=target_id,
                phase="input_preparation",
                input_id=fixture_id,
                artifact_path=artifact_path,
                resolve_credentials=replay,
                completed_at=_NOW,
                created_by="contract",
            )
            assert retry == fresh
            assert calls == 1
