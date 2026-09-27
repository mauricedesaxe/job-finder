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


@job(name="onboarding_test_search")
def _test_search_job() -> None:
    pass


def _queued_run_origin() -> RemoteJobOrigin:
    return RemoteJobOrigin(
        repository_origin=RemoteRepositoryOrigin(
            code_location_origin=InProcessCodeLocationOrigin(
                loadable_target_origin=LoadableTargetOrigin(
                    module_name="job_finder.test_dagster_schedule_overlap",
                    attribute="_test_search_job",
                )
            ),
            repository_name="onboarding_test_search_test_repo",
        ),
        job_name="onboarding_test_search",
    )


@pytest.mark.parametrize(
    "status",
    (
        DagsterRunStatus.QUEUED,
        DagsterRunStatus.STARTING,
        DagsterRunStatus.STARTED,
        DagsterRunStatus.CANCELING,
    ),
)
def test_unfinished_run_blocks_test_search_schedule(status: DagsterRunStatus) -> None:
    with DagsterInstance.local_temp() as instance:
        schedule = defs.get_schedule_def("onboarding_test_search_schedule")
        context = build_schedule_context(instance=instance)

        assert len(schedule.evaluate_tick(context).run_requests or []) == 1

        _ = instance.create_run_for_job(
            _test_search_job, status=status, remote_job_origin=_queued_run_origin()
        )

        tick = schedule.evaluate_tick(context)
        assert tick.run_requests == []
        assert tick.skip_message is not None


@pytest.mark.parametrize(
    "status",
    (
        DagsterRunStatus.SUCCESS,
        DagsterRunStatus.CANCELED,
        DagsterRunStatus.FAILURE,
    ),
)
def test_terminal_run_does_not_block_test_search_schedule(status: DagsterRunStatus) -> None:
    with DagsterInstance.local_temp() as instance:
        schedule = defs.get_schedule_def("onboarding_test_search_schedule")
        context = build_schedule_context(instance=instance)
        _ = instance.create_run_for_job(
            _test_search_job, status=status, remote_job_origin=_queued_run_origin()
        )

        assert len(schedule.evaluate_tick(context).run_requests or []) == 1
