# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false, reportUnannotatedClassAttribute=false
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
import requests

from job_finder.config import DagsterControlSettings
from job_finder.review.control_plane import (
    CONTROL_DEFINITIONS,
    ControlConflict,
    ControlPlaneUnavailable,
    RunLaunchUncertain,
    RunNowCommand,
    RunStarted,
    ScheduleChangeCommand,
    ScheduleChanged,
    ScheduleStateConflict,
    ScheduleStatus,
    dagster_control_plane_service,
    requests_graphql_transport,
)

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
SETTINGS = DagsterControlSettings.model_validate({"graphql_url": "http://dagster.test/graphql"})


class FakeGraphQL:
    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.statuses = {
            definition.schedule_name: ScheduleStatus.RUNNING for definition in CONTROL_DEFINITIONS
        }

    def __call__(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        if "OwnerSchedules" in query:
            return {"schedulesOrError": {"__typename": "Schedules", "results": self._schedules()}}
        if "OwnerRuns" in query:
            return {
                "runsOrError": {
                    "__typename": "Runs",
                    "results": [
                        {"runId": run_id, "jobName": "job_finder"} for run_id in self.run_ids
                    ],
                }
            }
        if "StartOwnerSchedule" in query:
            selector = cast(Mapping[str, object], variables["selector"])
            name = cast(str, selector["scheduleName"])
            self.statuses[name] = ScheduleStatus.RUNNING
            return self._mutation("startSchedule", ScheduleStatus.RUNNING)
        if "StopOwnerSchedule" in query:
            name = cast(str, variables["id"]).removeprefix("id:")
            self.statuses[name] = ScheduleStatus.STOPPED
            return self._mutation("stopRunningSchedule", ScheduleStatus.STOPPED)
        raise AssertionError(query)

    def _schedules(self) -> list[dict[str, object]]:
        return [
            {
                "id": f"id:{definition.schedule_name}",
                "name": definition.schedule_name,
                "cronSchedule": "* * * * *",
                "executionTimezone": "UTC",
                "scheduleState": {
                    "status": self.statuses[definition.schedule_name].value,
                    "nextTick": {"timestamp": 1_795_000_000.0},
                },
            }
            for definition in CONTROL_DEFINITIONS
        ]

    @staticmethod
    def _mutation(field: str, status: ScheduleStatus) -> Mapping[str, object]:
        return {
            field: {
                "__typename": "ScheduleStateResult",
                "scheduleState": {"status": status.value},
            }
        }


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


def _command(*, job_name: str = "job_finder", key: str = "private-key") -> RunNowCommand:
    return RunNowCommand(job_name=job_name, idempotency_key=key, actor="owner", timestamp=NOW)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"errors": [{"message": "failed"}]},
        {"data": None},
    ],
)
def test_http_transport_rejects_invalid_graphql_responses(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: FakeResponse(payload))

    with pytest.raises(ControlPlaneUnavailable):
        _ = requests_graphql_transport("http://dagster.test/graphql")("query Test", {})


def test_http_transport_maps_request_failures_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> FakeResponse:
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "post", fail)

    with pytest.raises(ControlPlaneUnavailable, match="request failed"):
        _ = requests_graphql_transport("http://dagster.test/graphql")("query Test", {})


def test_load_parses_every_allow_listed_schedule_and_next_tick() -> None:
    graphql = FakeGraphQL()
    service = dagster_control_plane_service(
        SETTINGS, graphql=graphql, launch_job=lambda *_args: "unused"
    )

    snapshot = service.load()

    assert tuple(view.definition for view in snapshot.schedules) == CONTROL_DEFINITIONS
    assert {view.status for view in snapshot.schedules} == {ScheduleStatus.RUNNING}
    assert all(view.next_tick is not None for view in snapshot.schedules)


def test_load_rejects_an_invalid_graphql_boundary() -> None:
    service = dagster_control_plane_service(
        SETTINGS,
        graphql=lambda _query, _variables: {"schedulesOrError": {"__typename": "PythonError"}},
        launch_job=lambda *_args: "unused",
    )

    with pytest.raises(ControlPlaneUnavailable, match="schedules are unavailable"):
        _ = service.load()


def test_run_now_rejects_a_job_outside_the_static_allow_list() -> None:
    launches: list[str] = []
    service = dagster_control_plane_service(
        SETTINGS,
        graphql=FakeGraphQL(),
        launch_job=lambda job, *_args: launches.append(job) or "run-1",
    )

    result = service.run_now(_command(job_name="not_a_job"))

    assert isinstance(result, ControlConflict)
    assert launches == []


def test_run_now_replays_the_single_run_matching_job_and_private_digest() -> None:
    graphql = FakeGraphQL()
    graphql.run_ids = ["run-existing"]
    launches: list[str] = []
    service = dagster_control_plane_service(
        SETTINGS,
        graphql=graphql,
        launch_job=lambda job, *_args: launches.append(job) or "run-new",
    )

    result = service.run_now(_command())

    assert result == RunStarted(run_id="run-existing", replayed=True)
    assert launches == []


def test_run_now_rejects_reusing_a_request_key_for_another_job() -> None:
    graphql = FakeGraphQL()
    graphql.run_ids = ["run-existing"]
    service = dagster_control_plane_service(
        SETTINGS, graphql=graphql, launch_job=lambda *_args: "unused"
    )

    result = service.run_now(_command(job_name="job_work_queue"))

    assert isinstance(result, ControlConflict)
    assert "another Dagster job" in result.reason


def test_run_now_reports_multiple_matching_runs_as_an_integrity_conflict() -> None:
    graphql = FakeGraphQL()
    graphql.run_ids = ["run-1", "run-2"]
    launches: list[str] = []
    service = dagster_control_plane_service(
        SETTINGS,
        graphql=graphql,
        launch_job=lambda job, *_args: launches.append(job) or "run-new",
    )

    result = service.run_now(_command())

    assert isinstance(result, ControlConflict)
    assert "Multiple Dagster runs" in result.reason
    assert launches == []


def test_sequential_run_now_retries_with_the_same_key_do_not_relaunch() -> None:
    graphql = FakeGraphQL()
    launches: list[Mapping[str, str]] = []

    def launch(_job: str, _location: str, _repository: str, tags: Mapping[str, str]) -> str:
        launches.append(tags)
        graphql.run_ids = ["run-new"]
        return "run-new"

    service = dagster_control_plane_service(SETTINGS, graphql=graphql, launch_job=launch)

    first = service.run_now(_command())
    second = service.run_now(_command())

    assert first == RunStarted(run_id="run-new", replayed=False)
    assert second == RunStarted(run_id="run-new", replayed=True)
    assert len(launches) == 1


def test_run_now_reconciles_after_a_launch_transport_error() -> None:
    graphql = FakeGraphQL()

    def launch(*_args: object) -> str:
        graphql.run_ids = ["run-after-timeout"]
        raise requests.Timeout("timed out")

    service = dagster_control_plane_service(SETTINGS, graphql=graphql, launch_job=launch)

    result = service.run_now(_command())

    assert result == RunStarted(run_id="run-after-timeout", replayed=True)


def test_run_now_returns_uncertain_with_no_run_after_a_launch_transport_error() -> None:
    def launch(*_args: object) -> str:
        raise requests.Timeout("timed out")

    service = dagster_control_plane_service(SETTINGS, graphql=FakeGraphQL(), launch_job=launch)

    result = service.run_now(_command())

    assert isinstance(result, RunLaunchUncertain)


def test_schedule_change_rejects_stale_state_without_mutating() -> None:
    graphql = FakeGraphQL()
    graphql.statuses["job_finder_schedule"] = ScheduleStatus.STOPPED
    service = dagster_control_plane_service(
        SETTINGS, graphql=graphql, launch_job=lambda *_args: "unused"
    )

    result = service.change_schedule(
        ScheduleChangeCommand(
            schedule_name="job_finder_schedule",
            expected_state=ScheduleStatus.RUNNING,
            desired_state=ScheduleStatus.STOPPED,
            actor="owner",
            timestamp=NOW,
        )
    )

    assert result == ScheduleStateConflict(
        expected=ScheduleStatus.RUNNING, observed=ScheduleStatus.STOPPED
    )
    assert graphql.statuses["job_finder_schedule"] is ScheduleStatus.STOPPED


def test_schedule_change_is_convergent_when_desired_state_is_current() -> None:
    graphql = FakeGraphQL()
    service = dagster_control_plane_service(
        SETTINGS, graphql=graphql, launch_job=lambda *_args: "unused"
    )

    result = service.change_schedule(
        ScheduleChangeCommand(
            schedule_name="job_finder_schedule",
            expected_state=ScheduleStatus.RUNNING,
            desired_state=ScheduleStatus.RUNNING,
            actor="owner",
            timestamp=NOW,
        )
    )

    assert result == ScheduleChanged(status=ScheduleStatus.RUNNING, replayed=True)
    assert graphql.statuses["job_finder_schedule"] is ScheduleStatus.RUNNING


def test_schedule_change_uses_the_public_mutation_and_verifies_its_state() -> None:
    graphql = FakeGraphQL()
    service = dagster_control_plane_service(
        SETTINGS, graphql=graphql, launch_job=lambda *_args: "unused"
    )

    result = service.change_schedule(
        ScheduleChangeCommand(
            schedule_name="job_finder_schedule",
            expected_state=ScheduleStatus.RUNNING,
            desired_state=ScheduleStatus.STOPPED,
            actor="owner",
            timestamp=NOW,
        )
    )

    assert result == ScheduleChanged(status=ScheduleStatus.STOPPED, replayed=False)
    assert graphql.statuses["job_finder_schedule"] is ScheduleStatus.STOPPED


def test_schedule_change_reconciles_a_mutation_error_from_authoritative_state() -> None:
    graphql = FakeGraphQL()
    original = graphql.__call__

    def execute(query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        if "StopOwnerSchedule" in query:
            graphql.statuses["job_finder_schedule"] = ScheduleStatus.STOPPED
            raise ControlPlaneUnavailable("response lost")
        return original(query, variables)

    service = dagster_control_plane_service(
        SETTINGS, graphql=execute, launch_job=lambda *_args: "unused"
    )

    result = service.change_schedule(
        ScheduleChangeCommand(
            schedule_name="job_finder_schedule",
            expected_state=ScheduleStatus.RUNNING,
            desired_state=ScheduleStatus.STOPPED,
            actor="owner",
            timestamp=NOW,
        )
    )

    assert result == ScheduleChanged(status=ScheduleStatus.STOPPED, replayed=True)
