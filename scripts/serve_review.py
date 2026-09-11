# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
from __future__ import annotations

import psycopg
from fasthtml.common import FastHTML

from job_finder.config import DatabaseSettings, ReviewAppSettings
from job_finder.database import apply_migrations
from job_finder.review import create_review_app, postgres_review_service


def create_app() -> FastHTML:
    database = DatabaseSettings.from_environment()
    settings = ReviewAppSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    with connect() as connection:
        _ = apply_migrations(connection)

    def readiness() -> None:
        with connect() as connection:
            _ = connection.execute("SELECT 1").fetchone()

    return create_review_app(
        postgres_review_service(connect),
        settings,
        readiness=readiness,
    )
