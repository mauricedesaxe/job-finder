from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import qualification_target_id
from job_finder.qualification_target_service import (
    CreateCurrentQualificationCandidateCommand,
    CreateQualificationCandidateCommand,
    create_qualification_candidate,
    create_current_qualification_candidate,
    get_active_qualification_authority,
    get_qualification_candidate,
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_qualification_service_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_candidate_requires_one_executing_artifact(authority_schema: str) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    root = Path(__file__).resolve().parents[1]
    settings = PostgresContractSettings.from_environment()
    with NamedTemporaryFile(
        dir=root, prefix=".qualification-candidate-", suffix=".json"
    ) as temporary:
        artifact_path = Path(temporary.name)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            _ = connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
            )
            _ = apply_migrations(connection)
            artifact, components, target = _store_default_qualification_target(connection, now)
            assert write_implementation_artifact(root, artifact_path) == artifact
            command = CreateQualificationCandidateCommand(
                input_preparation=components[0],
                relevance=components[1],
                enrichment=components[2],
                deduplication=components[3],
                actor="owner",
                timestamp=now,
            )
            created = create_qualification_candidate(connection, command, artifact_path)
            assert created.id == qualification_target_id(target)
            assert get_qualification_candidate(connection, created.id) == created
            assert create_qualification_candidate(connection, command, artifact_path) == created
            active = get_active_qualification_authority(connection)
            assert active.target_id is None and active.generation == 0
            wrong = command.model_copy(
                update={
                    "input_preparation": components[0].model_copy(update={"artifact_id": "f" * 64})
                }
            )
            with pytest.raises(ValueError, match="executing build artifact"):
                _ = create_qualification_candidate(connection, wrong, artifact_path)


def test_current_candidate_compiles_published_definition_without_activation(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    root = Path(__file__).resolve().parents[1]
    settings = PostgresContractSettings.from_environment()
    with NamedTemporaryFile(dir=root, prefix=".current-candidate-", suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            _ = connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
            )
            _ = apply_migrations(connection)
            _ = write_implementation_artifact(root, artifact_path)
            command = CreateCurrentQualificationCandidateCommand(actor="owner", timestamp=now)
            target = create_current_qualification_candidate(connection, command, artifact_path)
            assert (
                create_current_qualification_candidate(connection, command, artifact_path) == target
            )
            assert get_qualification_candidate(connection, target.id) == target
            active = get_active_qualification_authority(connection)
            assert active.target_id is None and active.generation == 0
            assert connection.execute(
                "SELECT count(*) FROM qualification_prompt_compilations WHERE target_id = %s",
                (target.id,),
            ).fetchone() == (1,)
