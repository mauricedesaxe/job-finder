from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from starlette.testclient import TestClient

from job_finder.acquisition_policy_service import (
    get_acquisition_policy_draft,
    get_active_acquisition_policy,
)
from job_finder.config import PostgresContractSettings, ReviewAppSettings
from job_finder.database import apply_migrations
from job_finder.database import ConnectionFactory
from job_finder.discovery.catalog import SupportedSearchSource
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.review.configuration_editor import postgres_configuration_editor_service
from job_finder.review.feedback import postgres_review_feedback_service
from job_finder.review.owner_access import (
    OnboardingStage,
    OwnerAccessService,
    OwnerAccessState,
    OwnerBootstrapConflict,
    postgres_owner_access_service,
)
from job_finder.review.queue import postgres_review_queue_service
from job_finder.qualification_definition_service import get_qualification_definition_draft
from job_finder.web.app import create_review_app


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_split_web_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def _client_for_schema(
    authority_schema: str, *, artifact_path: Path | None = None
) -> tuple[TestClient, ConnectionFactory]:
    settings = PostgresContractSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
        _ = connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
        )
        return connection

    owner_state = OwnerAccessState(stage=OnboardingStage.COMPLETE, has_password=True)
    owner = OwnerAccessService(
        load_state=lambda: owner_state,
        authenticate=lambda password: password == "owner password for this test",
        bootstrap=lambda _password: OwnerBootstrapConflict(owner_state),
    )
    app = create_review_app(
        postgres_review_queue_service(connect),
        postgres_configuration_editor_service(connect),
        ReviewAppSettings(
            session_secret="s" * 32,
            cookie_secure=False,
            split_execution_artifact_path=artifact_path or Path("/unused/artifact.json"),
        ),
        feedback_service=postgres_review_feedback_service(connect),
        owner_access_service=owner,
        split_configuration_connect=connect,
        now=lambda: datetime(2026, 9, 25, tzinfo=UTC),
    )
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"password": "owner password for this test", "next": "/configuration"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    return client, connect


def _publish_and_continue(
    client: TestClient,
    connect: ConnectionFactory,
    token: str,
    acquisition_version: int,
    qualification_version: int,
) -> None:
    acquisition_published = client.post(
        "/configuration/acquisition/publish",
        data={
            "csrf_token": token,
            "draft_version": str(acquisition_version),
            "idempotency_key": "acquisition-publication",
        },
        follow_redirects=False,
    )
    assert acquisition_published.status_code == 303
    qualification_published = client.post(
        "/configuration/qualification/publish",
        data={
            "csrf_token": token,
            "draft_version": str(qualification_version),
            "idempotency_key": "qualification-publication",
        },
        follow_redirects=False,
    )
    assert qualification_published.status_code == 303
    with connect() as connection:
        candidate_revision_id = get_acquisition_policy_draft(connection).base_revision_id
    activated = client.post(
        "/configuration/acquisition/activate",
        data={
            "csrf_token": token,
            "active_generation": "0",
            "idempotency_key": "acquisition-activation",
            "candidate_revision_id": candidate_revision_id,
        },
        follow_redirects=False,
    )
    assert activated.status_code == 303
    continued = client.post(
        "/configuration/continue", data={"csrf_token": token}, follow_redirects=False
    )
    assert continued.status_code == 303
    assert continued.headers["location"] == "/setup/budget"
    assert (
        client.post(
            "/configuration/continue", data={"csrf_token": token}, follow_redirects=False
        ).status_code
        == 409
    )
    with connect() as connection:
        assert connection.execute(
            "SELECT stage FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone() == ("budget",)


def test_owner_can_create_and_inspect_a_qualification_candidate(authority_schema: str) -> None:
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, prefix=".target-ui-", suffix=".json") as artifact_file:
        artifact_path = Path(artifact_file.name)
        _ = write_implementation_artifact(root, artifact_path)
        client, connect = _client_for_schema(authority_schema, artifact_path=artifact_path)
        with connect() as connection:
            _ = apply_migrations(connection)
        page = client.get("/configuration/qualification-targets")
        assert page.status_code == 200
        assert "No candidates yet" in page.text
        token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token_match is not None
        token = token_match.group(1)
        assert (
            client.post(
                "/configuration/qualification-targets/candidate",
                data={"csrf_token": "invalid"},
            ).status_code
            == 403
        )
        created = client.post(
            "/configuration/qualification-targets/candidate",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert created.status_code == 303
        with connect() as connection:
            row = connection.execute("SELECT id FROM qualification_targets").fetchone()
        assert row is not None
        candidate_id = str(row[0])
        assert candidate_id in client.get("/configuration/qualification-targets").text


def test_owner_promotion_page_previews_evidence_and_rejects_invalid_activation(
    authority_schema: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, prefix=".promotion-ui-", suffix=".json") as artifact_file:
        artifact_path = Path(artifact_file.name)
        _ = write_implementation_artifact(root, artifact_path)
        client, connect = _client_for_schema(authority_schema, artifact_path=artifact_path)
        with connect() as connection:
            _ = apply_migrations(connection)
        candidate_page = client.get("/configuration/qualification-targets")
        token_match = re.search(r'name="csrf_token" value="([^"]+)"', candidate_page.text)
        assert token_match is not None
        token = token_match.group(1)
        created = client.post(
            "/configuration/qualification-targets/candidate",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert created.status_code == 303
        with connect() as connection:
            row = connection.execute("SELECT id FROM qualification_targets").fetchone()
        assert row is not None
        candidate_id = str(row[0])
        page = client.get("/configuration/qualification-promotion")
        assert page.status_code == 200
        assert candidate_id in page.text
        assert "No canonical evidence recorded yet" in page.text
        assert (
            client.post(
                "/configuration/qualification-promotion/preview",
                data={"csrf_token": "invalid"},
            ).status_code
            == 403
        )
        preview = client.post(
            "/configuration/qualification-promotion/preview",
            data={
                "csrf_token": token,
                "baseline_target_id": candidate_id,
                "candidate_target_id": candidate_id,
            },
        )
        assert preview.status_code == 200
        assert "Evidence is incomplete" in preview.text
        assert "Candidate is the baseline target" in preview.text
        decision = client.post(
            "/configuration/qualification-promotion/decide",
            data={
                "csrf_token": token,
                "baseline_target_id": candidate_id,
                "candidate_target_id": candidate_id,
                "decision": "approved",
                "reason": "Browser contract",
                "idempotency_key": "same-target-decision",
            },
        )
        assert decision.status_code == 422
        assert "Candidate is the baseline target" in decision.text
        activation = client.post(
            "/configuration/qualification-promotion/activate",
            data={
                "csrf_token": token,
                "idempotency_key": "invalid-activation",
                "promotion_decision_id": "0" * 64,
                "expected_generation": "0",
            },
        )
        assert activation.status_code == 422
        assert "Approved qualification promotion does not exist" in activation.text


def test_owner_setup_writes_independent_search_drafts(authority_schema: str) -> None:
    client, connect = _client_for_schema(authority_schema)
    with connect() as connection:
        _ = apply_migrations(connection)
        qualification_before = get_qualification_definition_draft(connection)
        acquisition_before = get_acquisition_policy_draft(connection)
    _ = postgres_owner_access_service(connect).bootstrap("owner password for this test")
    page = client.get("/configuration")
    assert page.status_code == 200
    assert "Save acquisition draft" in page.text
    assert "Save qualification draft" in page.text
    assert "/configuration/draft" not in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    token = csrf.group(1)
    source = next(iter(SupportedSearchSource)).value
    saved = client.post(
        "/configuration/acquisition/draft",
        data={
            "csrf_token": token,
            "draft_version": str(acquisition_before.version),
            "search_keywords": "remote backend engineer\npython platform engineer",
            "enabled_sources": source,
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    with connect() as connection:
        acquisition_after = get_acquisition_policy_draft(connection)
        assert get_qualification_definition_draft(connection) == qualification_before
    assert acquisition_after.policy.search_keywords == (
        "remote backend engineer",
        "python platform engineer",
    )
    assert acquisition_after.version == acquisition_before.version + 1

    definition = qualification_before.definition.model_dump(mode="json")
    definition["personal_criteria"][0]["instructions"] = "Prefer remote engineering work."
    qualification_saved = client.post(
        "/configuration/qualification/draft",
        data={
            "csrf_token": token,
            "draft_version": str(qualification_before.version),
            "personal_criteria": json.dumps(definition["personal_criteria"]),
            "target_profiles": json.dumps(definition["target_profiles"]),
        },
        follow_redirects=False,
    )
    assert qualification_saved.status_code == 303
    with connect() as connection:
        assert get_acquisition_policy_draft(connection) == acquisition_after
        assert (
            get_qualification_definition_draft(connection).version
            == qualification_before.version + 1
        )
        assert (
            get_active_acquisition_policy(connection).revision.id
            == acquisition_before.base_revision_id
        )
        _ = connection.execute(
            "UPDATE owner_onboarding SET stage = 'preferences' WHERE singleton_id = 1"
        )

    _publish_and_continue(
        client, connect, token, acquisition_after.version, qualification_before.version + 1
    )


def test_invalid_split_draft_keeps_entered_values(authority_schema: str) -> None:
    client, connect = _client_for_schema(authority_schema)
    with connect() as connection:
        _ = apply_migrations(connection)
    page = client.get("/configuration")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    token = csrf.group(1)
    empty_acquisition = client.post(
        "/configuration/acquisition/draft",
        data={
            "csrf_token": token,
            "draft_version": "0",
            "search_keywords": "",
            "enabled_sources": next(iter(SupportedSearchSource)).value,
        },
    )
    assert empty_acquisition.status_code == 422
    assert "Enter at least one search keyword." in empty_acquisition.text
    invalid_acquisition = client.post(
        "/configuration/acquisition/draft",
        data={
            "csrf_token": token,
            "draft_version": "0",
            "search_keywords": "remote engineer\nremote engineer",
            "enabled_sources": next(iter(SupportedSearchSource)).value,
        },
    )
    assert invalid_acquisition.status_code == 422
    assert "remote engineer\nremote engineer" in invalid_acquisition.text
    assert "Search keywords must be unique" in invalid_acquisition.text
    invalid_qualification = client.post(
        "/configuration/qualification/draft",
        data={
            "csrf_token": token,
            "draft_version": "0",
            "personal_criteria": "not valid JSON",
            "target_profiles": "[]",
        },
    )
    assert invalid_qualification.status_code == 422
    assert "not valid JSON" in invalid_qualification.text
    assert "Enter valid JSON for the criteria and profiles" in invalid_qualification.text


def test_stale_acquisition_edit_keeps_submitted_keywords(authority_schema: str) -> None:
    client, connect = _client_for_schema(authority_schema)
    with connect() as connection:
        _ = apply_migrations(connection)
    page = client.get("/configuration")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    token = csrf.group(1)
    source = next(iter(SupportedSearchSource)).value
    first = client.post(
        "/configuration/acquisition/draft",
        data={
            "csrf_token": token,
            "draft_version": "0",
            "search_keywords": "backend engineer",
            "enabled_sources": source,
        },
    )
    assert first.status_code == 200
    stale = client.post(
        "/configuration/acquisition/draft",
        data={
            "csrf_token": token,
            "draft_version": "0",
            "search_keywords": "platform engineer",
            "enabled_sources": source,
        },
    )
    assert stale.status_code == 409
    assert "platform engineer" in stale.text
    with connect() as connection:
        assert get_acquisition_policy_draft(connection).policy.search_keywords == (
            "backend engineer",
        )


def test_search_setup_reports_database_outage_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_TEST_POSTGRES_DSN", "postgresql://test:test@127.0.0.1:5432/test")
    client, _ = _client_for_schema("unused")
    with patch("psycopg.connect", side_effect=psycopg.OperationalError("offline")):
        response = client.get("/configuration")

    assert response.status_code == 503
    assert "Search setup is unavailable" in response.text
    assert 'href="/configuration"' in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/configuration/acquisition/draft",
        "/configuration/acquisition/publish",
        "/configuration/acquisition/activate",
        "/configuration/qualification/draft",
        "/configuration/qualification/publish",
        "/configuration/continue",
    ],
)
def test_every_split_setup_write_checks_csrf_before_database_access(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("JOB_FINDER_TEST_POSTGRES_DSN", "postgresql://test:test@127.0.0.1:5432/test")
    client, _ = _client_for_schema("unused")
    with patch("psycopg.connect") as connect:
        response = client.post(path, data={"csrf_token": "invalid"})

    assert response.status_code == 403
    connect.assert_not_called()


@pytest.mark.parametrize(
    ("path", "fields"),
    [
        (
            "/configuration/acquisition/publish",
            {"draft_version": "0", "idempotency_key": "retry-acquisition-publish"},
        ),
        (
            "/configuration/qualification/publish",
            {"draft_version": "0", "idempotency_key": "retry-qualification-publish"},
        ),
        (
            "/configuration/acquisition/activate",
            {
                "active_generation": "0",
                "candidate_revision_id": "a" * 64,
                "idempotency_key": "retry-acquisition-activate",
            },
        ),
    ],
)
def test_uncertain_split_write_preserves_its_retry_command(
    authority_schema: str, path: str, fields: dict[str, str]
) -> None:
    client, connect = _client_for_schema(authority_schema)
    with connect() as connection:
        _ = apply_migrations(connection)
    page = client.get("/configuration")
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token_match is not None
    token = token_match.group(1)

    with patch("psycopg.connect", side_effect=psycopg.OperationalError("offline")):
        response = client.post(path, data={"csrf_token": token, **fields})

    assert response.status_code == 503
    assert "Search setup result is unknown" in response.text
    assert f'action="{path}"' in response.text
    for key, value in fields.items():
        assert f'name="{key}" value="{value}"' in response.text
