from __future__ import annotations

import sys
from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from scripts.backfill_review_queue import main as run_backfill
from scripts.reprocess_jobs import Connection, run_reprocess_command


@pytest.fixture
def script_schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    base_dsn = PostgresContractSettings.from_environment().postgres_dsn
    schema_name = f"job_finder_script_redaction_{uuid4().hex}"
    with psycopg.connect(base_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            monkeypatch.setenv(
                "JOB_FINDER_POSTGRES_DSN",
                f"{base_dsn}?options=-csearch_path%3D{schema_name}",
            )
            yield
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def _select_no_jobs(connection: Connection) -> tuple[UUID, ...]:
    return ()


def _reset_no_jobs(connection: Connection, job_ids: tuple[UUID, ...]) -> int:
    return 0


def test_reprocess_output_keeps_the_dsn_out_of_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script_schema: None,
) -> None:
    monkeypatch.setattr(sys, "argv", ["reprocess_jobs", "--dry-run"])
    run_reprocess_command("Requeue jobs for a test", _select_no_jobs, _reset_no_jobs)
    dry_run_output = capsys.readouterr().out
    assert "Dry run: 0 job(s) would be requeued" in dry_run_output

    monkeypatch.setattr(sys, "argv", ["reprocess_jobs"])
    run_reprocess_command("Requeue jobs for a test", _select_no_jobs, _reset_no_jobs)
    requeue_output = capsys.readouterr().out
    assert "Requeued 0 job(s)" in requeue_output

    for output in (dry_run_output, requeue_output):
        assert "postgres://" not in output
        assert "@" not in output


def test_backfill_output_keeps_the_dsn_out_of_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script_schema: None,
) -> None:
    monkeypatch.setattr(sys, "argv", ["backfill_review_queue", "--dry-run"])
    run_backfill()
    dry_run_output = capsys.readouterr().out
    assert "Dry run: 0 qualified decision(s) would enter the review queue" in dry_run_output

    monkeypatch.setattr(sys, "argv", ["backfill_review_queue"])
    run_backfill()
    enqueue_output = capsys.readouterr().out
    assert "Enqueued 0 review item(s)" in enqueue_output

    for output in (dry_run_output, enqueue_output):
        assert "postgres://" not in output
        assert "@" not in output
