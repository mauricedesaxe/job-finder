# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
from __future__ import annotations

import psycopg
from fasthtml.common import FastHTML

from job_finder.config import DagsterControlSettings, DatabaseSettings, ReviewAppSettings
from job_finder.database import apply_migrations
from job_finder.execution_budget import postgres_budget_setup_service
from job_finder.provider_credentials import (
    credential_cipher,
    postgres_provider_setup_service,
    production_provider_validators,
)
from job_finder.web.app import create_review_app
from job_finder.operations.control_plane import dagster_control_plane_service
from job_finder.review.feedback import postgres_review_submitter
from job_finder.operations.spend import postgres_analytics_service
from job_finder.operations.activity import postgres_activity_service
from job_finder.operations.run_history import postgres_runs_service
from job_finder.operations.service import postgres_operations_service
from job_finder.review.onboarding import postgres_test_search_service
from job_finder.review.owner_access import (
    OnboardingStage,
    import_legacy_owner_password,
    postgres_owner_access_service,
)
from job_finder.review.queue import postgres_review_queue_loader


def create_app() -> FastHTML:
    database = DatabaseSettings.from_environment()
    settings = ReviewAppSettings.from_environment()
    if settings.split_execution_artifact_path is None:
        raise RuntimeError("JOB_FINDER_ENABLE_SPLIT_EXECUTION must be true for owner search setup")
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
    budget_setup = postgres_budget_setup_service(connect)

    def readiness() -> None:
        _ = budget_setup.inspect(25)

    return create_review_app(
        postgres_review_queue_loader(connect),
        settings,
        submit_review=postgres_review_submitter(connect),
        owner_access_service=postgres_owner_access_service(connect),
        provider_setup_service=provider_setup,
        test_search_service=postgres_test_search_service(
            connect, artifact_path=settings.split_execution_artifact_path
        ),
        split_configuration_connect=connect,
        budget_setup_service=budget_setup,
        readiness=readiness,
        operations_service=postgres_operations_service(connect),
        runs_service=postgres_runs_service(connect),
        activity_service=postgres_activity_service(connect),
        analytics_service=postgres_analytics_service(connect),
        control_service=(None if dagster is None else dagster_control_plane_service(dagster)),
    )
