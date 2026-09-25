# pyright: reportMissingTypeStubs=false
"""Pin the production composition root against a real database.

create_review_app defaults every injectable service to a stub that raises
OperationsUnavailable at request time. serve_review.py builds those services
from Postgres; this contract proves the composition succeeds end to end.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable, cast

import pytest
from fasthtml.common import FastHTML

from job_finder.config import PostgresContractSettings

SERVE_REVIEW = Path(__file__).parents[1] / "scripts" / "serve_review.py"


def _load_serve_review_module() -> Callable[[], FastHTML]:
    spec = importlib.util.spec_from_file_location("serve_review", SERVE_REVIEW)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Callable[[], FastHTML], module.create_app)


def test_serve_review_composes_against_a_real_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = PostgresContractSettings.from_environment()
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", settings.postgres_dsn)
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "wiring-test-session-secret-0123456")
    monkeypatch.setenv("JOB_FINDER_BOOTSTRAP_TOKEN", "wiring-test-bootstrap-token-01234567")
    monkeypatch.setenv(
        "JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY",
        "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
    )

    create_app = _load_serve_review_module()
    app = create_app()

    assert app is not None
