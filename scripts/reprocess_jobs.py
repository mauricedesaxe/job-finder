from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from uuid import UUID

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations

Connection = psycopg.Connection[tuple[object, ...]]
JobSelector = Callable[[Connection], tuple[UUID, ...]]
JobResetter = Callable[[Connection, tuple[UUID, ...]], int]


class ReprocessArguments(argparse.Namespace):
    dry_run: bool = False
    limit: int = 0


def run_reprocess_command(
    description: str,
    select_jobs: JobSelector,
    reset_jobs: JobResetter,
) -> None:
    parser = argparse.ArgumentParser(description=description)
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what a reprocess would reset without changing any row",
    )
    _ = parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="process at most this many jobs (0 means all)",
    )
    arguments = parser.parse_args(namespace=ReprocessArguments())
    settings = DatabaseSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = apply_migrations(connection)
        job_ids = select_jobs(connection)
        if arguments.limit > 0:
            job_ids = job_ids[: arguments.limit]
        if arguments.dry_run:
            _print_preview(connection, job_ids)
            return
        reset = reset_jobs(connection, job_ids)
        _ = sys.stdout.write(f"Requeued {reset} job(s)\n")


def _print_preview(connection: Connection, job_ids: tuple[UUID, ...]) -> None:
    _ = sys.stdout.write(f"Dry run: {len(job_ids)} job(s) would be requeued\n")
    for job_id in job_ids[:20]:
        row = connection.execute(
            """
            SELECT s.company, s.title
            FROM job_snapshots s
            JOIN evaluation_decisions d ON d.snapshot_id = s.id
            WHERE s.job_id = %s
            ORDER BY d.created_at DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is not None:
            _ = sys.stdout.write(f"  {str(job_id)[:8]}  {row[0]}  |  {row[1]}\n")
    if len(job_ids) > 20:
        _ = sys.stdout.write(f"  ... and {len(job_ids) - 20} more\n")
