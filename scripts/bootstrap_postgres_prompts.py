from __future__ import annotations

import psycopg

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.evaluation import bootstrap_prompt_release


def main() -> None:
    settings = DatabaseSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
    print(f"Bootstrapped {release.name} ({release.id})")


if __name__ == "__main__":
    main()
