"""Stub Dagster code location for the control-plane contract test.

The real job bodies hit the network; their business behavior is covered by
contracts/test_dagster_orchestration.py via execute_in_process. This location
keeps the real job and schedule names so the review control plane can be
driven end-to-end against a real dagster-webserver GraphQL endpoint without
network side effects.
"""

from __future__ import annotations

from dagster import DefaultScheduleStatus, Definitions, JobDefinition, ScheduleDefinition, job, op


@op
def noop() -> None:
    return None


@job(name="job_finder")
def job_finder() -> None:
    noop()


@job(name="job_work_queue")
def job_work_queue() -> None:
    noop()


@job(name="review_sample")
def review_sample() -> None:
    noop()


@job(name="langfuse_projection")
def langfuse_projection() -> None:
    noop()


JOBS: tuple[JobDefinition, ...] = (job_finder, job_work_queue, review_sample, langfuse_projection)


defs = Definitions(
    jobs=JOBS,
    schedules=[
        ScheduleDefinition(
            job=job_definition,
            cron_schedule=cron_schedule,
            execution_timezone="UTC",
            default_status=DefaultScheduleStatus.RUNNING,
        )
        for job_definition, cron_schedule in zip(
            JOBS, ("0 7 * * *", "*/15 * * * *", "15 0 * * *", "* * * * *"), strict=True
        )
    ],
)
