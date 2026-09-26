from dagster import (
    DagsterInstance,
    DagsterRunStatus,
    build_schedule_context,
    job,  # pyright: ignore[reportUnknownVariableType]
)

from job_finder.dagster import defs


@job(name="onboarding_test_search")
def _test_search_job() -> None:
    pass


def test_test_search_schedule_waits_for_unfinished_run() -> None:
    with DagsterInstance.local_temp() as instance:
        schedule = defs.get_schedule_def("onboarding_test_search_schedule")
        context = build_schedule_context(instance=instance)

        assert len(schedule.evaluate_tick(context).run_requests or []) == 1

        _ = instance.create_run_for_job(_test_search_job, status=DagsterRunStatus.STARTED)

        tick = schedule.evaluate_tick(context)
        assert tick.run_requests == []
        assert tick.skip_message is not None


def test_terminal_run_does_not_block_test_search_schedule() -> None:
    with DagsterInstance.local_temp() as instance:
        schedule = defs.get_schedule_def("onboarding_test_search_schedule")
        context = build_schedule_context(instance=instance)
        _ = instance.create_run_for_job(_test_search_job, status=DagsterRunStatus.FAILURE)

        assert len(schedule.evaluate_tick(context).run_requests or []) == 1
