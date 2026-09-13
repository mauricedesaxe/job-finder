"""Backfill review items for qualified decisions that entered no review queue.

The queue now gains an item the moment a qualified decision persists, but
decisions recorded before that change never entered one. This script finds
those decisions and inserts the missing items with the same deterministic
ids the domain uses, so re-running inserts nothing new. Positions continue
each day's existing maximum, matching how the domain enqueues.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, uuid5

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations


class BackfillArguments(argparse.Namespace):
    dry_run: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create review items for qualified decisions that lack one"
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what a backfill would insert without changing any row",
    )
    arguments = parser.parse_args(namespace=BackfillArguments())
    settings = DatabaseSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = apply_migrations(connection)
        rows = connection.execute(
            """
            SELECT d.id, (d.created_at AT TIME ZONE 'UTC')::date
            FROM evaluation_decisions d
            LEFT JOIN review_items i ON i.evaluation_id = d.id
            WHERE d.outcome = 'qualified' AND i.id IS NULL
            ORDER BY (d.created_at AT TIME ZONE 'UTC')::date, d.created_at, d.id
            """
        ).fetchall()
        if arguments.dry_run:
            print(f"Dry run: {len(rows)} qualified decision(s) would enter the review queue")
            for row in rows[:20]:
                sample = connection.execute(
                    """
                    SELECT s.company, s.title
                    FROM evaluation_decisions d
                    JOIN job_snapshots s ON s.id = d.snapshot_id
                    WHERE d.id = %s
                    """,
                    (str(row[0]),),
                ).fetchone()
                if sample is not None:
                    print(f"  {row[1]}  {sample[0]}  |  {sample[1]}")
            if len(rows) > 20:
                print(f"  ... and {len(rows) - 20} more")
            return
        positions = {
            _as_date(row[0]).isoformat(): int(str(row[1]))
            for row in connection.execute(
                """
                SELECT review_day, COALESCE(max(position), -1)
                FROM review_items
                WHERE lane = 'qualified'
                GROUP BY review_day
                """
            ).fetchall()
        }
        inserted = 0
        for row in rows:
            evaluation_id = str(row[0])
            review_day = _as_date(row[1])
            day = review_day.isoformat()
            positions[day] = positions.get(day, -1) + 1
            item_id = uuid5(
                NAMESPACE_URL,
                f"daily-review:{day}:qualified:{evaluation_id}",
            )
            inserted += connection.execute(
                """
                INSERT INTO review_items (
                  id, evaluation_id, review_day, lane, position, created_at
                ) VALUES (%s, %s, %s, 'qualified', %s, %s)
                ON CONFLICT (evaluation_id) DO NOTHING
                """,
                (item_id, evaluation_id, review_day, positions[day], datetime.now(UTC)),
            ).rowcount
        print(f"Enqueued {inserted} review item(s) on {connection.info.dsn}")


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


if __name__ == "__main__":
    main()
