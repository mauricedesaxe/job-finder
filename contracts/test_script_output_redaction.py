from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PASSWORD = "hunter2"
SCRIPT_WRONG_PASSWORD = "not-the-script-password"


@pytest.fixture
def script_postgres_dsn() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    identity = f"job_finder_script_redaction_{uuid4().hex}"
    role = identity[:63]
    schema = identity[:63]
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        host = connection.info.host
        port = connection.info.port
        dbname = connection.info.dbname
        _ = connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(SCRIPT_PASSWORD)
            )
        )
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        try:
            yield (
                f"postgresql://{role}:{SCRIPT_PASSWORD}@{host}:{port}/{dbname}"
                f"?options=-csearch_path%3D{schema}"
            )
        finally:
            _ = connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            _ = connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _run_script(module: str, dsn: str, *, dry_run: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *(["--dry-run"] if dry_run else [])],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env={**os.environ, "JOB_FINDER_POSTGRES_DSN": dsn},
        check=False,
    )


@pytest.mark.parametrize(
    ("module", "dry_run", "expected"),
    [
        (
            "scripts.backfill_review_queue",
            True,
            "Dry run: 0 qualified decision(s) would enter the review queue",
        ),
        ("scripts.backfill_review_queue", False, "Enqueued 0 review item(s)"),
        ("scripts.reprocess_thin_body_jobs", True, "Dry run: 0 job(s) would be requeued"),
        ("scripts.reprocess_thin_body_jobs", False, "Requeued 0 job(s)"),
    ],
)
def test_script_output_keeps_database_credentials_private(
    script_postgres_dsn: str, module: str, dry_run: bool, expected: str
) -> None:
    result = _run_script(module, script_postgres_dsn, dry_run=dry_run)

    assert result.returncode == 0
    assert expected in result.stdout
    assert SCRIPT_PASSWORD not in result.stdout + result.stderr
    assert "postgresql://" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "scripts.backfill_review_queue",
        "scripts.reprocess_thin_body_jobs",
        "scripts.backfill_snapshots",
        "scripts.reprocess_mis_titled_jobs",
        "scripts.rebuild_langfuse_projections",
        "scripts.bootstrap_postgres_prompts",
    ],
)
def test_script_failure_output_keeps_database_credentials_private(
    script_postgres_dsn: str, module: str
) -> None:
    rejected = script_postgres_dsn.replace(f":{SCRIPT_PASSWORD}@", f":{SCRIPT_WRONG_PASSWORD}@")

    result = _run_script(module, rejected, dry_run=True)

    assert result.returncode != 0
    assert SCRIPT_PASSWORD not in result.stdout + result.stderr
    assert SCRIPT_WRONG_PASSWORD not in result.stdout + result.stderr
    assert "postgresql://" not in result.stdout + result.stderr
