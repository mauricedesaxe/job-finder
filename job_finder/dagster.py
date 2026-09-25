from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import requests
from dagster import (
    AssetExecutionContext,
    ConfigurableResource,
    DefaultScheduleStatus,
    Definitions,
    RetryPolicy,
    ScheduleDefinition,
    asset,
    define_asset_job,  # pyright: ignore[reportUnknownVariableType]
)

from job_finder.config import DatabaseSettings, LangfuseSettings, OrchestrationSettings
from job_finder.database import apply_migrations
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot, fetch_exchange_rates
from job_finder.execution_budget import (
    ExecutionAdmitted,
    ExecutionBlocked,
    admit_scheduled_execution,
    reserve_discovery,
    reserve_job_capacity,
    settle_execution_budget,
)
from job_finder.onboarding_test_search_worker import execute_next_onboarding_test_search
from job_finder.projections.langfuse import create_langfuse_projection_sender
from job_finder.projections.outbox import (
    ProjectionDelivered,
    ProjectionFailed,
    ProjectionIdle,
    deliver_next_projection,
)
from job_finder.pipeline.orchestration import (
    DiscoverySummary,
    PipelineBoundaries,
    ProcessingSummary,
    discover_jobs,
    process_claimed_jobs,
    production_boundaries,
)
from job_finder.pipeline.connection import Connection
from job_finder.pipeline.runs import (
    OrchestrationRun,
    complete_orchestration_run,
    fail_orchestration_run,
    prepare_orchestration_run,
)
from job_finder.provider_credentials import (
    ExecutionProviderCredentials,
    credential_cipher,
    resolve_execution_provider_credentials,
)
from job_finder.review.queue import enqueue_rejected_audit_sample

HeartbeatSender = Callable[[str], None]


class JobFinderResource(ConfigurableResource["JobFinderResource"]):
    @contextmanager
    def connection(self) -> Generator[Connection, None, None]:
        settings = DatabaseSettings.from_environment()
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            _ = apply_migrations(connection)
            yield connection


@asset(
    pool="job_finder_pipeline",
)
def job_finder_cycle(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, int]:
    observed_at = datetime.now(UTC)
    settings = OrchestrationSettings.from_environment()
    with job_finder.connection() as connection:
        run_key = f"dagster:{context.run.run_id}"
        admission = admit_scheduled_execution(
            connection, idempotency_key=run_key, requested_at=observed_at
        )
        ping_heartbeat(settings.discovery_heartbeat_url)
        if isinstance(admission, ExecutionBlocked):
            metadata = {"blocked": 1}
            context.add_output_metadata({**metadata, "reason": admission.reason})
            return metadata
        credentials = _provider_credentials(connection, settings)
        boundaries = production_boundaries(jina_api_key=credentials.jina.get_secret_value())
        run: OrchestrationRun | None = None
        work_started = False
        try:
            run = _prepare_run(connection, context, settings, admission, observed_at)
            work_started = True
            if reserve_discovery(connection, run_key):
                discovery = discover_jobs(
                    connection,
                    run,
                    boundaries,
                    discovered_at=observed_at,
                    max_workers=settings.search_worker_count,
                )
                discovery.require_complete()
            else:
                discovery = DiscoverySummary(
                    query_count=0,
                    unavailable_query_count=0,
                    discovered_count=0,
                    new_work_count=0,
                )
            processing = _process_admitted_batch(
                connection, run_key, run, boundaries, credentials, settings, observed_at
            )
            complete_orchestration_run(connection, run.id, completed_at=datetime.now(UTC))
            settle_execution_budget(
                connection,
                idempotency_key=run_key,
                pipeline_run_id=run.id,
                settled_at=datetime.now(UTC),
                consume_allowance=True,
            )
            ping_heartbeat(settings.discovery_heartbeat_url)
        except Exception as error:
            if run is not None:
                _fail_run(connection, run, error)
            settle_execution_budget(
                connection,
                idempotency_key=run_key,
                pipeline_run_id=None if run is None else run.id,
                settled_at=datetime.now(UTC),
                consume_allowance=work_started,
            )
            raise
    metadata = {
        "queries": discovery.query_count,
        "unavailable_queries": discovery.unavailable_query_count,
        "discovered": discovery.discovered_count,
        "new_work": discovery.new_work_count,
        **_processing_metadata(processing),
    }
    context.add_output_metadata(metadata)
    return metadata


@asset(
    pool="job_finder_pipeline",
)
def job_work_queue_cycle(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, int]:
    observed_at = datetime.now(UTC)
    settings = OrchestrationSettings.from_environment()
    with job_finder.connection() as connection:
        run_key = f"dagster:{context.run.run_id}"
        admission = admit_scheduled_execution(
            connection, idempotency_key=run_key, requested_at=observed_at
        )
        ping_heartbeat(settings.work_queue_heartbeat_url)
        if isinstance(admission, ExecutionBlocked):
            metadata = {"blocked": 1}
            context.add_output_metadata({**metadata, "reason": admission.reason})
            return metadata
        credentials = _provider_credentials(connection, settings)
        boundaries = production_boundaries(jina_api_key=credentials.jina.get_secret_value())
        run: OrchestrationRun | None = None
        work_started = False
        try:
            run = _prepare_run(connection, context, settings, admission, observed_at)
            work_started = True
            processing = _process_admitted_batch(
                connection, run_key, run, boundaries, credentials, settings, observed_at
            )
            complete_orchestration_run(connection, run.id, completed_at=datetime.now(UTC))
            settle_execution_budget(
                connection,
                idempotency_key=run_key,
                pipeline_run_id=run.id,
                settled_at=datetime.now(UTC),
                consume_allowance=processing.claimed_count > 0,
            )
            ping_heartbeat(settings.work_queue_heartbeat_url)
        except Exception as error:
            if run is not None:
                _fail_run(connection, run, error)
            settle_execution_budget(
                connection,
                idempotency_key=run_key,
                pipeline_run_id=None if run is None else run.id,
                settled_at=datetime.now(UTC),
                consume_allowance=work_started,
            )
            raise
    metadata = _processing_metadata(processing)
    context.add_output_metadata(metadata)
    return metadata


@asset(pool="job_finder_pipeline")
def onboarding_test_search_cycle(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, object]:
    settings = OrchestrationSettings.from_environment()
    with job_finder.connection() as connection:
        pending = connection.execute(
            """
            SELECT 1 FROM onboarding_test_search_requests
            WHERE state = 'pending' OR (state = 'leased' AND lease_expires_at <= %s)
            LIMIT 1
            """,
            (datetime.now(UTC),),
        ).fetchone()
        if pending is None:
            return {"state": "idle"}
        credentials = _provider_credentials(connection, settings)
        result = execute_next_onboarding_test_search(
            connection,
            production_boundaries(jina_api_key=credentials.jina.get_secret_value()),
            implementation_ref=settings.implementation_ref,
            openrouter_api_key=credentials.openrouter.get_secret_value(),
            typesafe_api_key=credentials.typesafe.get_secret_value(),
            owner_token=uuid4(),
            lease_for=timedelta(seconds=settings.work_lease_seconds),
            retry_after=timedelta(seconds=settings.work_retry_seconds),
            enable_ats_enrichment=settings.enable_ats_enrichment,
            fetch_rates=lambda: _fetch_rates(datetime.now(UTC)),
        )
    metadata: dict[str, object] = {
        "state": result.state,
        "queries": result.queries,
        "urls": result.urls,
        "jobs": result.jobs,
        "provider_attempts": result.provider_attempts,
    }
    if result.request_key is not None:
        metadata["request_key"] = result.request_key
    context.add_output_metadata(metadata)
    return metadata


@asset(
    pool="job_finder_pipeline",
    retry_policy=RetryPolicy(max_retries=2, delay=60),
)
def review_audit_sample(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, object]:
    review_day = (datetime.now(UTC) - timedelta(days=1)).date()
    with job_finder.connection() as connection:
        enqueued = enqueue_rejected_audit_sample(connection, review_day=review_day)
    metadata: dict[str, object] = {"enqueued": enqueued, "review_day": review_day.isoformat()}
    context.add_output_metadata(metadata)
    return metadata


@asset(pool="langfuse_projection")
def langfuse_projection_queue(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, int]:
    settings = LangfuseSettings.from_environment()
    sender = create_langfuse_projection_sender(settings)
    delivered = 0
    failed = 0
    lease_lost = 0
    with job_finder.connection() as connection:
        for _ in range(100):
            result = deliver_next_projection(
                connection,
                sender=sender,
                owner_token=uuid4(),
                now=datetime.now(UTC),
                lease_for=timedelta(minutes=5),
                retry_after=timedelta(hours=1),
            )
            if isinstance(result, ProjectionIdle):
                break
            if isinstance(result, ProjectionDelivered):
                delivered += 1
            elif isinstance(result, ProjectionFailed):
                failed += 1
            else:
                lease_lost += 1
    metadata = {"delivered": delivered, "failed": failed, "lease_lost": lease_lost}
    context.add_output_metadata(metadata)
    if failed:
        raise RuntimeError(f"Langfuse projection failed for {failed} items")
    return metadata


def _prepare_run(
    connection: Connection,
    context: AssetExecutionContext,
    settings: OrchestrationSettings,
    admission: ExecutionAdmitted,
    observed_at: datetime,
) -> OrchestrationRun:
    return prepare_orchestration_run(
        connection,
        idempotency_key=f"dagster:{context.run.run_id}",
        implementation_ref=settings.implementation_ref,
        configuration_revision_id=admission.configuration_revision_id,
        target=admission.target,
        started_at=observed_at,
        fetch_rates=lambda: _fetch_rates(observed_at),
    )


def _process_admitted_batch(
    connection: Connection,
    run_key: str,
    run: OrchestrationRun,
    boundaries: PipelineBoundaries,
    credentials: ExecutionProviderCredentials,
    settings: OrchestrationSettings,
    observed_at: datetime,
) -> ProcessingSummary:
    admitted_max_jobs = reserve_job_capacity(connection, run_key)
    if not admitted_max_jobs:
        return ProcessingSummary(
            claimed_count=0,
            terminal_count=0,
            terminal_error_count=0,
            retry_scheduled_count=0,
            lease_lost_count=0,
        )
    return process_claimed_jobs(
        connection,
        run,
        boundaries,
        openrouter_api_key=credentials.openrouter.get_secret_value(),
        typesafe_api_key=credentials.typesafe.get_secret_value(),
        owner_token=uuid4(),
        observed_at=observed_at,
        max_items=min(settings.work_batch_size, admitted_max_jobs),
        lease_for=timedelta(seconds=settings.work_lease_seconds),
        retry_after=timedelta(seconds=settings.work_retry_seconds),
        enable_ats_enrichment=settings.enable_ats_enrichment,
    )


def _provider_credentials(
    connection: Connection,
    settings: OrchestrationSettings,
) -> ExecutionProviderCredentials:
    cipher = (
        None
        if settings.credential_encryption_key is None
        else credential_cipher(settings.credential_encryption_key)
    )
    return resolve_execution_provider_credentials(
        connection,
        cipher=cipher,
        jina_fallback=settings.jina_api_key,
        openrouter_fallback=settings.openrouter_api_key,
        typesafe_fallback=settings.typesafe_api_key,
    )


def _processing_metadata(processing: ProcessingSummary) -> dict[str, int]:
    return {
        "claimed": processing.claimed_count,
        "terminal": processing.terminal_count,
        "terminal_error": processing.terminal_error_count,
        "retry_scheduled": processing.retry_scheduled_count,
        "lease_lost": processing.lease_lost_count,
    }


def _fail_run(connection: Connection, run: OrchestrationRun, error: Exception) -> None:
    fail_orchestration_run(
        connection,
        run.id,
        completed_at=datetime.now(UTC),
        error_code=type(error).__name__,
        reason=str(error),
    )


def ping_heartbeat(url: str | None, sender: HeartbeatSender | None = None) -> None:
    if url is None:
        return
    try:
        _ = (sender or _default_heartbeat_sender)(url)
    except Exception:
        pass


def _default_heartbeat_sender(url: str) -> None:
    _ = requests.get(url, timeout=5)


def _fetch_rates(observed_at: datetime) -> ExchangeRateSnapshot:
    return fetch_exchange_rates(observed_at=observed_at)


job_finder_job = define_asset_job("job_finder", selection=[job_finder_cycle.key])
job_work_queue_job = define_asset_job("job_work_queue", selection=[job_work_queue_cycle.key])
onboarding_test_search_job = define_asset_job(
    "onboarding_test_search", selection=[onboarding_test_search_cycle.key]
)
langfuse_projection_job = define_asset_job(
    "langfuse_projection", selection=[langfuse_projection_queue.key]
)
review_sample_job = define_asset_job("review_sample", selection=[review_audit_sample.key])
review_sample_schedule = ScheduleDefinition(
    job=review_sample_job,
    cron_schedule="15 0 * * *",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.RUNNING,
)
job_finder_schedule = ScheduleDefinition(
    job=job_finder_job,
    cron_schedule="0 7 * * *",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.RUNNING,
)
job_work_queue_schedule = ScheduleDefinition(
    job=job_work_queue_job,
    cron_schedule="*/15 * * * *",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.RUNNING,
)
onboarding_test_search_schedule = ScheduleDefinition(
    job=onboarding_test_search_job,
    cron_schedule="* * * * *",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.RUNNING,
)
langfuse_projection_schedule = ScheduleDefinition(
    job=langfuse_projection_job,
    cron_schedule="* * * * *",
    execution_timezone="UTC",
    default_status=(
        DefaultScheduleStatus.RUNNING
        if LangfuseSettings.credentials_are_configured()
        else DefaultScheduleStatus.STOPPED
    ),
)

defs = Definitions(
    assets=[
        job_finder_cycle,
        job_work_queue_cycle,
        onboarding_test_search_cycle,
        review_audit_sample,
        langfuse_projection_queue,
    ],
    jobs=[
        job_finder_job,
        job_work_queue_job,
        onboarding_test_search_job,
        review_sample_job,
        langfuse_projection_job,
    ],
    schedules=[
        job_finder_schedule,
        job_work_queue_schedule,
        onboarding_test_search_schedule,
        review_sample_schedule,
        langfuse_projection_schedule,
    ],
    resources={"job_finder": JobFinderResource()},
)
