"""Re-enqueue every Langfuse projection from PostgreSQL authority rows.

Langfuse is a rebuildable projection: its data is a deterministic function of
domain rows. This script re-derives every projection payload from those rows
and inserts it as a pending projection item; the scheduled drain delivers it.
Safe to re-run at any time — projection identity is stable, and the enqueue
writes are ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.projections.rebuild import rebuild_langfuse_projections


def main() -> int:
    settings = DatabaseSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = apply_migrations(connection)
        counts = rebuild_langfuse_projections(connection)
    summary = ", ".join(f"{count} {name}" for name, count in counts.items())
    print(f"re-enqueued {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
