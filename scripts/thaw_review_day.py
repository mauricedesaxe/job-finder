"""Thaw a frozen daily review day so the next open re-freezes its membership.

Deleting the review_days row and its review_items makes every evaluation of
that UTC day eligible again; prepare_daily_review re-selects them on the next
open. A day whose items already carry submitted reviews is refused.
"""

from __future__ import annotations

import argparse
from datetime import date

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.review import thaw_review_day

Connection = psycopg.Connection[tuple[object, ...]]


class ThawArguments(argparse.Namespace):
    day: date = date(1970, 1, 1)
    dry_run: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Thaw a frozen daily review day so the next open re-freezes membership"
    )
    _ = parser.add_argument("day", type=date.fromisoformat, help="review day to thaw (YYYY-MM-DD)")
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what a thaw would delete without changing any row",
    )
    arguments = parser.parse_args(namespace=ThawArguments())
    settings = DatabaseSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        if arguments.dry_run:
            _print_plan(connection, arguments.day)
            return
        apply_migrations(connection)
        deleted_items, deleted_days = thaw_review_day(connection, arguments.day)
        dsn = connection.info.dsn
    if (deleted_items, deleted_days) == (0, 0):
        print(f"Nothing to thaw for {arguments.day.isoformat()}: no day row or review items")
        return
    deleted = f"{deleted_items} review items and {deleted_days} review day row(s)"
    print(f"Thawed {arguments.day.isoformat()} on {dsn}: {deleted}")


def _print_plan(connection: Connection, review_day: date) -> None:
    rows = connection.execute(
        """
        SELECT lane, count(*)
        FROM review_items
        WHERE review_day = %s
        GROUP BY lane
        ORDER BY lane
        """,
        (review_day,),
    ).fetchall()
    submitted_row = connection.execute(
        """
        SELECT count(*)
        FROM review_events e
        JOIN review_items i ON i.id = e.review_item_id
        WHERE i.review_day = %s
        """,
        (review_day,),
    ).fetchone()
    submitted = int(str(submitted_row[0])) if submitted_row is not None else 0
    per_lane = ", ".join(f"{str(lane)}: {str(count)}" for lane, count in rows) or "no review items"
    print(f"Dry run for {review_day.isoformat()}: {per_lane}; submitted reviews: {submitted}")


if __name__ == "__main__":
    main()
