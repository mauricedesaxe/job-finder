"""Requeue jobs that were decided against broken scrapes the ATS backfill repaired.

Thin scrapes (Jina intermittently returns HTTP 200 with an empty body on
JS-rendered ATS pages) produced decisions made with no usable posting text.
Deleting those decisions and resetting the work items to pending makes the
regular queue drain re-scrape every selected job and decide it again under
the current prompt release, now composing the canonical ATS description and
structured compensation. Decisions quoted by review items are left alone, as
are jobs holding any other decision.
"""

from __future__ import annotations

import argparse

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.pipeline import reset_thin_body_jobs, select_thin_body_jobs


class ReprocessArguments(argparse.Namespace):
    dry_run: bool = False
    limit: int = 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Requeue jobs decided against thin scrapes that the ATS backfill repaired"
    )
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
        job_ids = select_thin_body_jobs(connection)
        if arguments.limit > 0:
            job_ids = job_ids[: arguments.limit]
        if arguments.dry_run:
            print(f"Dry run: {len(job_ids)} job(s) would be requeued")
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
                    print(f"  {str(job_id)[:8]}  {row[0]}  |  {row[1]}")
            if len(job_ids) > 20:
                print(f"  ... and {len(job_ids) - 20} more")
            return
        reset = reset_thin_body_jobs(connection, job_ids)
        print(f"Requeued {reset} job(s) on {connection.info.dsn}")


if __name__ == "__main__":
    main()
