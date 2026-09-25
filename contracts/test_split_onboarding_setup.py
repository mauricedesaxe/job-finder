# Why: Reuse the legacy onboarding fixture so this contract tests the same owner state.
# pyright: reportPrivateUsage=false
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from contracts.test_onboarding_test_search import (
    _connection,
    _prepare_test_search_owner,
)
from job_finder.config import PostgresContractSettings
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.onboarding_test_search import (
    OnboardingTestSearchAccepted,
    SplitOnboardingTestSearchRequest,
)
from job_finder.review.onboarding import postgres_test_search_service


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_split_setup_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_owner_setup_launches_split_candidate_search(authority_schema: str) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, prefix=".split-setup-", suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        _ = write_implementation_artifact(root, artifact_path)
        service = postgres_test_search_service(
            lambda: _connection(authority_schema), artifact_path=artifact_path
        )
        result = service.launch("owner", now)
        assert isinstance(result, OnboardingTestSearchAccepted)
        assert isinstance(result.request, SplitOnboardingTestSearchRequest)
        assert result.request.qualification_generation == 0
        replay = service.launch("owner", now)
        assert isinstance(replay, OnboardingTestSearchAccepted)
        assert replay.request == result.request
        with _connection(authority_schema) as connection:
            assert connection.execute(
                "SELECT count(*) FROM qualification_prompt_compilations WHERE target_id = %s",
                (result.request.qualification_target_id,),
            ).fetchone() == (1,)
