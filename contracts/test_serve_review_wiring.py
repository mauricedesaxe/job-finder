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
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

import psycopg
import pytest
from fasthtml.common import FastHTML
from psycopg import sql
from starlette.routing import Route
from starlette.testclient import TestClient

from job_finder.config import PostgresContractSettings

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


def test_serve_review_requires_split_owner_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://unused")
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "wiring-test-session-secret-0123456")
    monkeypatch.delenv("JOB_FINDER_ENABLE_SPLIT_EXECUTION", raising=False)

    create_app = _load_serve_review_module()
    with pytest.raises(RuntimeError, match="JOB_FINDER_ENABLE_SPLIT_EXECUTION"):
        _ = create_app()
