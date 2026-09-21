# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
from __future__ import annotations

import psycopg
from fasthtml.common import FastHTML

from job_finder.config import DagsterControlSettings, DatabaseSettings, ReviewAppSettings
from job_finder.database import apply_migrations
from job_finder.review.app import create_review_app
from job_finder.review.configuration_editor import postgres_configuration_editor_service
from job_finder.review.control_plane import dagster_control_plane_service
from job_finder.review.operations import postgres_operations_service
from job_finder.review.postgres import postgres_review_service


def create_app() -> FastHTML:
    database = DatabaseSettings.from_environment()
    settings = ReviewAppSettings.from_environment()
    dagster = DagsterControlSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    with connect() as connection:
        _ = apply_migrations(connection)

    def readiness() -> None:
        with connect() as connection:
            _ = connection.execute("SELECT 1").fetchone()

    return create_review_app(
        postgres_review_service(connect),
        postgres_configuration_editor_service(connect),
        settings,
        readiness=readiness,
        operations_service=postgres_operations_service(connect),
        control_service=dagster_control_plane_service(dagster),
    )
