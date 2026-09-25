from __future__ import annotations

import os

import psycopg


def main() -> None:
    dsn = os.environ["JOB_FINDER_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection:
        _ = connection.execute("CREATE SCHEMA IF NOT EXISTS dagster")


if __name__ == "__main__":
    main()
