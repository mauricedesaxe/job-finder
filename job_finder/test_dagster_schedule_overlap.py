import pytest
from dagster import (
    DagsterInstance,
    DagsterRunStatus,
    build_schedule_context,
    job,  # pyright: ignore[reportUnknownVariableType]
)
from dagster._core.remote_origin import (  # pyright: ignore[reportPrivateImportUsage]
    InProcessCodeLocationOrigin,
    RemoteJobOrigin,
    RemoteRepositoryOrigin,
)
from dagster._core.types.loadable_target_origin import (  # pyright: ignore[reportPrivateImportUsage]
    LoadableTargetOrigin,
)

from job_finder.dagster import defs


@job(name="review_sample")
def _review_sample_job() -> None:
    pass


@job(name="job_finder")
def _job_finder_job() -> None:
    pass


@job(name="job_work_queue")
def _job_work_queue_job() -> None:
    pass


@job(name="onboarding_test_search")
def _onboarding_test_search_job() -> None:
    pass


@job(name="langfuse_projection")
def _langfuse_projection_job() -> None:
    pass


_STUB_JOBS = {
    "review_sample": _review_sample_job,
    "job_finder": _job_finder_job,
    "job_work_queue": _job_work_queue_job,
    "onboarding_test_search": _onboarding_test_search_job,
    "langfuse_projection": _langfuse_projection_job,
}

_SCHEDULES = {
    "review_sample": "review_sample_schedule",
    "job_finder": "job_finder_schedule",
    "job_work_queue": "job_work_queue_schedule",
    "onboarding_test_search": "onboarding_test_search_schedule",
    "langfuse_projection": "langfuse_projection_schedule",
}


def _queued_run_origin(job_name: str) -> RemoteJobOrigin:
    return RemoteJobOrigin(
        repository_origin=RemoteRepositoryOrigin(
            code_location_origin=InProcessCodeLocationOrigin(
                loadable_target_origin=LoadableTargetOrigin(
                    module_name="job_finder.test_dagster_schedule_overlap",
                    attribute=job_name,
                )
            ),
            repository_name=f"{job_name}_test_repo",
        ),
        job_name=job_name,
    )


@pytest.mark.parametrize("job_name", tuple(_SCHEDULES))
@pytest.mark.parametrize(
    "status",
    (
        DagsterRunStatus.QUEUED,
        DagsterRunStatus.STARTING,
        DagsterRunStatus.STARTED,
        DagsterRunStatus.CANCELING,
    ),
)
def test_unfinished_run_blocks_the_schedule(job_name: str, status: DagsterRunStatus) -> None:
    with DagsterInstance.local_temp() as instance:
        schedule = defs.get_schedule_def(_SCHEDULES[job_name])
        context = build_schedule_context(instance=instance)

        assert len(schedule.evaluate_tick(context).run_requests or []) == 1

        _ = instance.create_run_for_job(
            _STUB_JOBS[job_name], status=status, remote_job_origin=_queued_run_origin(job_name)
        )

        tick = schedule.evaluate_tick(context)
        assert tick.run_requests == []
        assert tick.skip_message is not None


@pytest.mark.parametrize("job_name", tuple(_SCHEDULES))
@pytest.mark.parametrize(
    "status",
    (
        DagsterRunStatus.SUCCESS,
        DagsterRunStatus.CANCELED,
        DagsterRunStatus.FAILURE,
    ),
)
def test_terminal_run_does_not_block_the_schedule(job_name: str, status: DagsterRunStatus) -> None:
    with DagsterInstance.local_temp() as instance:
        schedule = defs.get_schedule_def(_SCHEDULES[job_name])
        context = build_schedule_context(instance=instance)
        _ = instance.create_run_for_job(
            _STUB_JOBS[job_name], status=status, remote_job_origin=_queued_run_origin(job_name)
        )

        assert len(schedule.evaluate_tick(context).run_requests or []) == 1
