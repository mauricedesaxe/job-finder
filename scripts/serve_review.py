# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
from __future__ import annotations

import psycopg
from fasthtml.common import FastHTML

from job_finder.config import DagsterControlSettings, DatabaseSettings, ReviewAppSettings
from job_finder.database import apply_migrations
from job_finder.provider_credentials import (
    credential_cipher,
    postgres_provider_setup_service,
    production_provider_validators,
)
from job_finder.review.app import create_review_app
from job_finder.review.configuration_editor import postgres_configuration_editor_service
from job_finder.review.control_plane import dagster_control_plane_service
from job_finder.review.operations import postgres_operations_service
from job_finder.review.owner_access import (
    OnboardingStage,
    import_legacy_owner_password,
    postgres_owner_access_service,
)
from job_finder.review.postgres import postgres_review_service


def create_app() -> FastHTML:
    database = DatabaseSettings.from_environment()
    settings = ReviewAppSettings.from_environment()
    dagster = DagsterControlSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    with connect() as connection:
        _ = apply_migrations(connection)
        owner_state = import_legacy_owner_password(
            connection,
            None
            if settings.legacy_password is None
            else settings.legacy_password.get_secret_value(),
        )
        if owner_state.stage is OnboardingStage.OWNER_ACCOUNT and settings.bootstrap_token is None:
            raise RuntimeError(
                "JOB_FINDER_BOOTSTRAP_TOKEN is required until the owner account is created"
            )
        if (
            owner_state.stage
            not in (
                OnboardingStage.LEGACY_OWNER_IMPORT,
                OnboardingStage.COMPLETE,
            )
            and settings.credential_encryption_key is None
        ):
            raise RuntimeError("JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY is required during onboarding")

    provider_setup = (
        None
        if settings.credential_encryption_key is None
        else postgres_provider_setup_service(
            connect,
            credential_cipher(settings.credential_encryption_key),
            production_provider_validators(),
        )
    )

    def readiness() -> None:
        with connect() as connection:
            _ = connection.execute("SELECT 1").fetchone()

    return create_review_app(
        postgres_review_service(connect),
        postgres_configuration_editor_service(connect),
        settings,
        owner_access_service=postgres_owner_access_service(connect),
        provider_setup_service=provider_setup,
        readiness=readiness,
        operations_service=postgres_operations_service(connect),
        control_service=dagster_control_plane_service(dagster),
    )
