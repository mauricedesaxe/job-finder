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

from job_finder.config import LangfuseSettings, OrchestrationSettings
from job_finder.database import apply_migrations
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot, fetch_exchange_rates
from job_finder.evaluation.langfuse import (
    ProjectionDelivered,
    ProjectionFailed,
    ProjectionIdle,
    create_langfuse_projection_sender,
    deliver_next_projection,
)
from job_finder.evaluation.prompt_releases import bootstrap_prompt_release
from job_finder.pipeline.orchestration import (
    PipelineBoundaries,
    ProcessingSummary,
    discover_jobs,
    process_claimed_jobs,
    production_boundaries,
)
from job_finder.pipeline.state import (
    Connection,
    OrchestrationRun,
    complete_orchestration_run,
    fail_orchestration_run,
    prepare_orchestration_run,
)

HeartbeatSender = Callable[[str], None]


class JobFinderResource(ConfigurableResource["JobFinderResource"]):
    @contextmanager
    def connection(self) -> Generator[Connection, None, None]:
        settings = OrchestrationSettings.from_environment()
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
            _ = apply_migrations(connection)
            yield connection


@asset(
    pool="job_finder_pipeline",
    retry_policy=RetryPolicy(max_retries=2, delay=60),
)
def job_finder_cycle(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, int]:
    settings = OrchestrationSettings.from_environment()
    observed_at = datetime.now(UTC)
    boundaries = production_boundaries(jina_api_key=settings.jina_api_key)
    with job_finder.connection() as connection:
        run = _prepare_run(connection, context, settings, observed_at)
        try:
            discovery = discover_jobs(
                connection,
                run,
                boundaries,
                discovered_at=observed_at,
                max_workers=settings.search_worker_count,
            )
            discovery.require_complete()
            processing = _process_batch(connection, run, boundaries, settings, observed_at)
            complete_orchestration_run(connection, run.id, completed_at=datetime.now(UTC))
            ping_heartbeat(settings.discovery_heartbeat_url)
        except Exception as error:
            _fail_run(connection, run, error)
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
    retry_policy=RetryPolicy(max_retries=2, delay=60),
)
def job_work_queue_cycle(
    context: AssetExecutionContext, job_finder: JobFinderResource
) -> dict[str, int]:
    settings = OrchestrationSettings.from_environment()
    observed_at = datetime.now(UTC)
    boundaries = production_boundaries(jina_api_key=settings.jina_api_key)
    with job_finder.connection() as connection:
        run = _prepare_run(connection, context, settings, observed_at)
        try:
            processing = _process_batch(connection, run, boundaries, settings, observed_at)
            complete_orchestration_run(connection, run.id, completed_at=datetime.now(UTC))
            ping_heartbeat(settings.work_queue_heartbeat_url)
        except Exception as error:
            _fail_run(connection, run, error)
            raise
    metadata = _processing_metadata(processing)
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
    observed_at: datetime,
) -> OrchestrationRun:
    return prepare_orchestration_run(
        connection,
        idempotency_key=f"dagster:{context.run.run_id}",
        implementation_ref=settings.implementation_ref,
        started_at=observed_at,
        load_prompt_release=bootstrap_prompt_release,
        fetch_rates=lambda: _fetch_rates(observed_at),
    )


def _process_batch(
    connection: Connection,
    run: OrchestrationRun,
    boundaries: PipelineBoundaries,
    settings: OrchestrationSettings,
    observed_at: datetime,
) -> ProcessingSummary:
    return process_claimed_jobs(
        connection,
        run,
        boundaries,
        openrouter_api_key=settings.openrouter_api_key,
        owner_token=uuid4(),
        observed_at=observed_at,
        max_items=settings.work_batch_size,
        lease_for=timedelta(seconds=settings.work_lease_seconds),
        retry_after=timedelta(seconds=settings.work_retry_seconds),
        enable_ats_enrichment=settings.enable_ats_enrichment,
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
langfuse_projection_job = define_asset_job(
    "langfuse_projection", selection=[langfuse_projection_queue.key]
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
    assets=[job_finder_cycle, job_work_queue_cycle, langfuse_projection_queue],
    jobs=[job_finder_job, job_work_queue_job, langfuse_projection_job],
    schedules=[
        job_finder_schedule,
        job_work_queue_schedule,
        langfuse_projection_schedule,
    ],
    resources={"job_finder": JobFinderResource()},
)
