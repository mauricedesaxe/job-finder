"""Requeue jobs that were refused because a mis-titled scrape hid the real role.

Deleting the refusal decisions and resetting the work items to pending makes
the regular queue drain re-scrape every selected job with the reader-title
fix and decide it again. Decisions quoted by review items are left alone,
as are jobs holding any other decision. A job whose page still yields no
usable title will be refused again with the same reason and re-selected on
the next run; that churn is expected, so do not re-run to be sure.
"""

from __future__ import annotations

import argparse

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.pipeline import reset_mis_titled_jobs, select_mis_titled_jobs


class ReprocessArguments(argparse.Namespace):
    dry_run: bool = False
    limit: int = 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Requeue jobs refused for titles that the reader-title fix repairs"
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
        job_ids = select_mis_titled_jobs(connection)
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
        reset = reset_mis_titled_jobs(connection, job_ids)
        print(f"Requeued {reset} job(s) on {connection.info.dsn}")


if __name__ == "__main__":
    main()
