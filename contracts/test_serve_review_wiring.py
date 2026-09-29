# pyright: reportMissingTypeStubs=false
"""Pin the production composition root against a real database.

create_review_app defaults every injectable service to a stub that raises
OperationsUnavailable at request time. serve_review.py builds those services
from Postgres; this contract proves the composition serves real requests.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from socket import socket
from typing import Callable, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from fasthtml.common import FastHTML
from psycopg import sql
from starlette.routing import Route
from starlette.testclient import TestClient

from job_finder.config import (
    DatabaseSettings,
    PostgresContractSettings,
    ReviewAppSettings,
)
from job_finder.database import apply_migrations
from job_finder.execution_budget import postgres_budget_setup_service
from job_finder.review.accounts import AccountService
from job_finder.review.feedback import postgres_review_submitter
from job_finder.review.owner_access import postgres_owner_access_service
from job_finder.review.queue import load_review_queue, postgres_review_queue_loader
from job_finder.web.app import create_review_app

SERVE_REVIEW = Path(__file__).parents[1] / "scripts" / "serve_review.py"
OWNER_PASSWORD = "wiring test owner password"


def _refused_postgres_dsn() -> str:
    with socket() as probe:
        probe.bind(("127.0.0.1", 0))
        _host, port = cast(tuple[str, int], probe.getsockname())
    return f"postgresql://jobfinder:hunter2@127.0.0.1:{port}/jobfinder_test"


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


@pytest.fixture
def wiring_schema() -> Generator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_wiring_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def _load_serve_review_module() -> Callable[[], FastHTML]:
    spec = importlib.util.spec_from_file_location("serve_review", SERVE_REVIEW)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Callable[[], FastHTML], module.create_app)


def test_serve_review_serves_real_requests_from_the_environment(
    wiring_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = PostgresContractSettings.from_environment()
    monkeypatch.setenv(
        "JOB_FINDER_POSTGRES_DSN",
        f"{settings.postgres_dsn}?options=-csearch_path%3D{wiring_schema}",
    )
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "wiring-test-session-secret-0123456")
    monkeypatch.setenv("JOB_FINDER_BOOTSTRAP_TOKEN", "wiring-test-bootstrap-token-01234567")
    monkeypatch.setenv(
        "JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY",
        "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
    )

    monkeypatch.setenv("JOB_FINDER_ENABLE_SPLIT_EXECUTION", "true")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_ARTIFACT", "/unused/artifact.json")

    create_app = _load_serve_review_module()
    app = create_app()
    assert app is not None
    paths = {route.path for route in app.routes if isinstance(route, Route)}
    assert "/configuration/acquisition/draft" in paths
    assert "/configuration/qualification/draft" in paths
    assert "/configuration/draft" not in paths

    client = TestClient(app, base_url="https://testserver")
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    guarded = client.get("/operations/runs", follow_redirects=False)
    assert guarded.status_code == 303
    assert guarded.headers["location"] == "/setup"

    setup_form = client.get("/setup")
    csrf_match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', setup_form.text)
    assert csrf_match is not None
    claim = client.post(
        "/setup",
        data={
            "csrf_token": csrf_match.group(1),
            "bootstrap_token": "wiring-test-bootstrap-token-01234567",
            "email": "admin@example.com",
            "password": OWNER_PASSWORD,
            "password_confirmation": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )
    assert claim.status_code == 303
    assert claim.headers["location"] == "/setup/providers"

    setup = client.get("/setup/providers")
    assert setup.status_code == 200
    assert "provider" in setup.text.lower()


def test_readyz_reports_database_unavailability_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", _refused_postgres_dsn())
    database = DatabaseSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    budget = postgres_budget_setup_service(connect)

    def readiness() -> None:
        _ = budget.inspect(25)

    app = create_review_app(
        postgres_review_queue_loader(connect),
        ReviewAppSettings(session_secret="s" * 32, cookie_secure=False),
        submit_review=postgres_review_submitter(connect),
        account_service=AccountService(connect),
        owner_access_service=postgres_owner_access_service(connect),
        readiness=readiness,
    )
    client = TestClient(app, base_url="https://testserver")

    ready = client.get("/readyz")
    health = client.get("/healthz")

    assert ready.status_code == 503
    assert ready.text == "database unavailable"
    assert health.status_code == 200
    assert health.text == "ok"
    assert "hunter2" not in ready.text + health.text


def test_serve_review_requires_split_owner_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://unused")
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "wiring-test-session-secret-0123456")
    monkeypatch.delenv("JOB_FINDER_ENABLE_SPLIT_EXECUTION", raising=False)

    create_app = _load_serve_review_module()
    with pytest.raises(RuntimeError, match="JOB_FINDER_ENABLE_SPLIT_EXECUTION"):
        _ = create_app()


_WIRING_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _seed_qualified_queue_item(
    connection: psycopg.Connection[tuple[object, ...]],
) -> tuple[UUID, str, str]:
    release_id = f"{uuid4().int:064x}"
    version_id = f"{uuid4().int:064x}"
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, criterion, phase, content_digest, messages,
              input_schema, output_schema, model, parameters, created_at
            ) VALUES (%s, 'contract-prompt', 'contract', 'enrichment', %s, '[]'::jsonb,
              '{}'::jsonb, '{}'::jsonb, 'test/model', '{}'::jsonb, %s)
            """,
            (version_id, f"{uuid4().int:064x}", _WIRING_NOW),
        )
        connection.execute(
            """
            INSERT INTO prompt_releases (
              id, name, content_digest, expected_member_count, created_at, created_by
            ) VALUES (%s, %s, %s, 1, %s, 'contract')
            """,
            (release_id, f"release-{uuid4().hex[:8]}", f"{uuid4().int:064x}", _WIRING_NOW),
        )
        connection.execute(
            """
            INSERT INTO prompt_release_members (
              release_id, prompt_name, prompt_version_id, position
            ) VALUES (%s, 'contract-prompt', %s, 0)
            """,
            (release_id, version_id),
        )
    run_id = uuid4()
    connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
          status, started_at, completed_at
        ) VALUES (%s, %s, 'processing', 'contract-ref', %s, '{}'::jsonb, 'completed', %s, %s)
        """,
        (run_id, f"wiring-review:{run_id}", release_id, _WIRING_NOW, _WIRING_NOW),
    )
    job_id = UUID(int=9001)
    snapshot_id = f"{9101:064x}"
    evaluation_id = f"{9901:064x}"
    raw_url = "https://example.com/jobs/wiring-review"
    connection.execute(
        """
        INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
        VALUES (%s, %s, %s, %s)
        """,
        (job_id, raw_url, _WIRING_NOW, _WIRING_NOW),
    )
    connection.execute(
        """
        INSERT INTO job_snapshots (
          id, job_id, content_digest, title, company, normalized_company,
          normalized_title, source, raw_url, description, location, keywords,
          date_posted, observed_at
        ) VALUES (%s, %s, %s, %s, 'Acme', 'acme', %s, 'other', %s,
          %s, 'Remote', '["python"]'::jsonb, %s, %s)
        """,
        (
            snapshot_id,
            job_id,
            f"{9201:064x}",
            "Wiring Review Engineer",
            "wiring review engineer",
            raw_url,
            "## Overview\nBuild useful tools.",
            _WIRING_NOW.date(),
            _WIRING_NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO evaluation_decisions (
          id, snapshot_id, pipeline_run_id, prompt_release_id, policy_version,
          outcome, matched_profile, reason, created_at
        ) VALUES (%s, %s, %s, %s, 'policy-1', 'qualified', 'applied-ai-product-engineer',
          'Matches the target profile.', %s)
        """,
        (evaluation_id, snapshot_id, run_id, release_id, _WIRING_NOW),
    )
    item_id = uuid4()
    connection.execute(
        """
        INSERT INTO review_items (
          id, evaluation_id, review_day, lane, position, created_at
        ) VALUES (%s, %s, %s, 'qualified', 0, %s)
        """,
        (item_id, evaluation_id, _WIRING_NOW.date(), _WIRING_NOW),
    )
    return item_id, evaluation_id, snapshot_id


def test_web_review_submission_persists_the_real_review_event(wiring_schema: str) -> None:
    settings = PostgresContractSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
        _ = connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(wiring_schema))
        )
        return connection

    with connect() as connection:
        _ = apply_migrations(connection)
        accounts = AccountService(connect)
        assert accounts.first_admin("admin@example.com", "wiring-admin-password") is not None
        for stage in ("preferences", "budget", "test_search", "complete"):
            _ = connection.execute(
                "UPDATE owner_onboarding SET stage = %s WHERE singleton_id = 1", (stage,)
            )
        item_id, evaluation_id, snapshot_id = _seed_qualified_queue_item(connection)

    app = create_review_app(
        postgres_review_queue_loader(connect),
        ReviewAppSettings(session_secret="s" * 32, cookie_secure=False),
        submit_review=postgres_review_submitter(connect),
        account_service=accounts,
        owner_access_service=postgres_owner_access_service(connect),
    )
    client = TestClient(app)
    login_form = client.get("/login")
    csrf_match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', login_form.text)
    assert csrf_match is not None
    login = client.post(
        "/login",
        data={
            "csrf_token": csrf_match.group(1),
            "email": "admin@example.com",
            "password": "wiring-admin-password",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303

    queue_page = client.get("/")
    assert queue_page.status_code == 200
    assert "Wiring Review Engineer" in queue_page.text
    item_page = client.get(f"/review/item/{item_id}")
    assert item_page.status_code == 200
    submitted = client.post(
        f"/review/{item_id}",
        data={
            "csrf_token": _csrf(item_page.text),
            "evaluation_id": evaluation_id,
            "snapshot_id": snapshot_id,
            "decision": "pursue",
            "note": "Strong wiring fit.",
            "block_company": "on",
        },
        follow_redirects=False,
    )

    assert submitted.status_code == 303
    assert submitted.headers["location"] == "/"

    with connect() as connection:
        event = connection.execute(
            "SELECT decision, note, block_company, actor FROM review_events"
        ).fetchone()
        assert event == ("pursue", "Strong wiring fit.", True, "admin@example.com")
        assert connection.execute(
            "SELECT policy FROM company_policies WHERE normalized_company = 'acme'"
        ).fetchone() == ("blocked",)
        assert connection.execute("SELECT count(*) FROM review_items").fetchone() == (1,)
        queue_after = load_review_queue(connection)
    assert queue_after.items == ()
    assert [reviewed.decision for reviewed in queue_after.reviewed_items] == ["pursue"]
    assert queue_after.reviewed_items[0].block_company is True
