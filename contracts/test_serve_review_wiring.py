# pyright: reportMissingTypeStubs=false
"""Pin the production composition root against a real database.

create_review_app defaults every injectable service to a stub that raises
OperationsUnavailable at request time. serve_review.py builds those services
from Postgres; this contract proves the composition serves real requests.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Generator
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

import psycopg
import pytest
from fasthtml.common import FastHTML
from psycopg import sql
from starlette.testclient import TestClient

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.review.owner_access import postgres_owner_access_service

SERVE_REVIEW = Path(__file__).parents[1] / "scripts" / "serve_review.py"
OWNER_PASSWORD = "wiring test owner password"


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

    create_app = _load_serve_review_module()
    app = create_app()
    assert app is not None

    client = TestClient(app, base_url="https://testserver")
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
        _ = connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(wiring_schema))
        )
        return connection

    with connect() as connection:
        _ = apply_migrations(connection)
    _ = postgres_owner_access_service(connect).bootstrap(OWNER_PASSWORD)

    guarded = client.get("/operations/runs", follow_redirects=False)
    assert guarded.status_code == 303
    assert guarded.headers["location"].startswith("/login")

    login = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": "/setup/providers"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    setup = client.get("/setup/providers")
    assert setup.status_code == 200
    assert "provider" in setup.text.lower()
