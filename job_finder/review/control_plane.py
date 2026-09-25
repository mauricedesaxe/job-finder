from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn, cast
from urllib.parse import urlsplit

import requests
from dagster_graphql import DagsterGraphQLClient, DagsterGraphQLClientError

from job_finder.config import DagsterControlSettings

GraphQLTransport = Callable[[str, Mapping[str, object]], Mapping[str, object]]
JobLauncher = Callable[[str, str, str, Mapping[str, str]], str]

OWNER_HOME_CORRELATION_TAG = "job_finder/owner_home_request"
OWNER_HOME_ACTOR_TAG = "job_finder/owner_home_actor"
OWNER_HOME_SOURCE_TAG = "job_finder/owner_home_source"
OWNER_HOME_SOURCE = "owner-home"


class ScheduleStatus(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class ControlDefinition:
    job_name: str
    schedule_name: str
    label: str
    cadence: str


CONTROL_DEFINITIONS = (
    ControlDefinition("job_finder", "job_finder_schedule", "Full pipeline", "Daily at 07:00 UTC"),
    ControlDefinition(
        "job_work_queue",
        "job_work_queue_schedule",
        "Work queue",
        "Every 15 minutes",
    ),
    ControlDefinition(
        "onboarding_test_search",
        "onboarding_test_search_schedule",
        "Setup test search",
        "Every minute while setup is active",
    ),
    ControlDefinition(
        "review_sample",
        "review_sample_schedule",
        "Review sample",
        "Daily at 00:15 UTC",
    ),
    ControlDefinition(
        "langfuse_projection",
        "langfuse_projection_schedule",
        "Langfuse projection",
        "Every minute",
    ),
)
_JOBS = {definition.job_name: definition for definition in CONTROL_DEFINITIONS}
_SCHEDULES = {definition.schedule_name: definition for definition in CONTROL_DEFINITIONS}


@dataclass(frozen=True)
class ScheduleView:
    definition: ControlDefinition
    status: ScheduleStatus
    next_tick: datetime | None


@dataclass(frozen=True)
class ControlPlaneSnapshot:
    schedules: tuple[ScheduleView, ...]


@dataclass(frozen=True)
class RunNowCommand:
    job_name: str
    idempotency_key: str
    actor: str
    timestamp: datetime


@dataclass(frozen=True)
class RunStarted:
    run_id: str
    replayed: bool


@dataclass(frozen=True)
class RunLaunchUncertain:
    reason: str


@dataclass(frozen=True)
class ControlConflict:
    reason: str


@dataclass(frozen=True)
class ControlError:
    reason: str


RunNowResult = RunStarted | RunLaunchUncertain | ControlConflict | ControlError


@dataclass(frozen=True)
class ScheduleChangeCommand:
    schedule_name: str
    expected_state: ScheduleStatus
    desired_state: ScheduleStatus
    actor: str
    timestamp: datetime


@dataclass(frozen=True)
class ScheduleChanged:
    status: ScheduleStatus
    replayed: bool


@dataclass(frozen=True)
class ScheduleStateConflict:
    expected: ScheduleStatus
    observed: ScheduleStatus


ScheduleChangeResult = ScheduleChanged | ScheduleStateConflict | ControlConflict | ControlError


@dataclass(frozen=True)
class ControlPlaneService:
    load: Callable[[], ControlPlaneSnapshot]
    run_now: Callable[[RunNowCommand], RunNowResult]
    change_schedule: Callable[[ScheduleChangeCommand], ScheduleChangeResult]


class ControlPlaneUnavailable(RuntimeError):
    pass


def unavailable_control_plane_service() -> ControlPlaneService:
    def unavailable(*_args: object) -> NoReturn:
        raise ControlPlaneUnavailable("Dagster control plane is unavailable")

    return ControlPlaneService(load=unavailable, run_now=unavailable, change_schedule=unavailable)


def requests_graphql_transport(url: str, *, timeout: float = 5) -> GraphQLTransport:
    def execute(query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        try:
            response = requests.post(
                url,
                json={"query": query, "variables": dict(variables)},
                timeout=timeout,
            )
            response.raise_for_status()
            payload: object = response.json()  # pyright: ignore[reportAny]
        except (requests.RequestException, ValueError) as error:
            raise ControlPlaneUnavailable("Dagster GraphQL request failed") from error
        if not isinstance(payload, dict):
            raise ControlPlaneUnavailable("Dagster GraphQL returned an invalid response")
        typed_payload = cast(Mapping[str, object], payload)
        if typed_payload.get("errors"):
            raise ControlPlaneUnavailable("Dagster GraphQL returned an error")
        data = typed_payload.get("data")
        if not isinstance(data, dict):
            raise ControlPlaneUnavailable("Dagster GraphQL response has no data")
        return cast(Mapping[str, object], data)

    return execute


def dagster_job_launcher(settings: DagsterControlSettings) -> JobLauncher:
    parsed = urlsplit(str(settings.graphql_url))
    client = DagsterGraphQLClient(
        parsed.hostname or "",
        port_number=parsed.port,
        use_https=parsed.scheme == "https",
        timeout=settings.timeout_seconds,
        path_prefix=parsed.path.removesuffix("/graphql"),
    )

    def launch(
        job_name: str,
        repository_location_name: str,
        repository_name: str,
        tags: Mapping[str, str],
    ) -> str:
        return client.submit_job_execution(
            job_name,
            repository_location_name=repository_location_name,
            repository_name=repository_name,
            tags=dict(tags),
        )

    return launch


def dagster_control_plane_service(
    settings: DagsterControlSettings,
    *,
    graphql: GraphQLTransport | None = None,
    launch_job: JobLauncher | None = None,
) -> ControlPlaneService:
    execute = graphql or requests_graphql_transport(
        str(settings.graphql_url), timeout=settings.timeout_seconds
    )
    launch = launch_job or dagster_job_launcher(settings)

    def load() -> ControlPlaneSnapshot:
        rows = _read_schedule_rows(execute, settings)
        return ControlPlaneSnapshot(
            schedules=tuple(_schedule_view(rows, definition) for definition in CONTROL_DEFINITIONS)
        )

    def run_now(command: RunNowCommand) -> RunNowResult:
        if command.job_name not in _JOBS:
            return ControlConflict("Job is not allow-listed")
        digest = _correlation_digest(command)
        try:
            matches = _matching_runs(execute, digest)
        except ControlPlaneUnavailable as error:
            return ControlError(str(error))
        existing = _existing_run_result(matches, command.job_name)
        if existing is not None:
            return existing
        try:
            run_id = launch(
                command.job_name,
                settings.repository_location_name,
                settings.repository_name,
                {
                    OWNER_HOME_CORRELATION_TAG: digest,
                    OWNER_HOME_ACTOR_TAG: command.actor,
                    OWNER_HOME_SOURCE_TAG: OWNER_HOME_SOURCE,
                },
            )
        except (requests.RequestException, DagsterGraphQLClientError):
            try:
                matches = _matching_runs(execute, digest)
            except ControlPlaneUnavailable:
                return RunLaunchUncertain("The launch result could not be reconciled")
            existing = _existing_run_result(matches, command.job_name)
            if existing is not None:
                return existing
            return RunLaunchUncertain("Dagster may have accepted the launch")
        if not run_id:
            return ControlError("Dagster returned an invalid run identifier")
        return RunStarted(run_id=run_id, replayed=False)

    def change_schedule(command: ScheduleChangeCommand) -> ScheduleChangeResult:
        if command.schedule_name not in _SCHEDULES:
            return ControlConflict("Schedule is not allow-listed")
        try:
            row = _read_schedule_rows(execute, settings).get(command.schedule_name)
        except ControlPlaneUnavailable as error:
            return ControlError(str(error))
        if row is None:
            return ControlError("Dagster did not return the allow-listed schedule")
        try:
            observed = _schedule_status(row)
        except ControlPlaneUnavailable as error:
            return ControlError(str(error))
        if observed is not command.expected_state:
            return ScheduleStateConflict(expected=command.expected_state, observed=observed)
        if observed is command.desired_state:
            return ScheduleChanged(status=observed, replayed=True)
        try:
            changed = _mutate_schedule(execute, settings, row, command.desired_state)
        except ControlPlaneUnavailable as error:
            return _reconcile_schedule_change(execute, settings, command, str(error))
        if changed is not command.desired_state:
            return ControlError(
                f"Dagster returned {changed.value} after requesting {command.desired_state.value}"
            )
        return _reconcile_schedule_change(execute, settings, command, None)

    return ControlPlaneService(load=load, run_now=run_now, change_schedule=change_schedule)


_SCHEDULES_QUERY = """
query OwnerSchedules($repository: RepositorySelector!) {
  schedulesOrError(repositorySelector: $repository) {
    __typename
    ... on Schedules {
      results {
        id
        name
        cronSchedule
        executionTimezone
        scheduleState { status nextTick { timestamp } }
      }
    }
  }
}
"""

_RUNS_QUERY = """
query OwnerRuns($filter: RunsFilter!) {
  runsOrError(filter: $filter, limit: 2) {
    __typename
    ... on Runs { results { runId jobName } }
  }
}
"""

_START_SCHEDULE_MUTATION = """
mutation StartOwnerSchedule($selector: ScheduleSelector!) {
  startSchedule(scheduleSelector: $selector) {
    __typename
    ... on ScheduleStateResult { scheduleState { status } }
  }
}
"""

_STOP_SCHEDULE_MUTATION = """
mutation StopOwnerSchedule($id: String!) {
  stopRunningSchedule(id: $id) {
    __typename
    ... on ScheduleStateResult { scheduleState { status } }
  }
}
"""


def _repository_selector(settings: DagsterControlSettings) -> dict[str, str]:
    return {
        "repositoryLocationName": settings.repository_location_name,
        "repositoryName": settings.repository_name,
    }


def _read_schedule_rows(
    execute: GraphQLTransport, settings: DagsterControlSettings
) -> dict[str, Mapping[str, object]]:
    data = execute(_SCHEDULES_QUERY, {"repository": _repository_selector(settings)})
    result = _mapping(data.get("schedulesOrError"), "schedulesOrError")
    if result.get("__typename") != "Schedules":
        raise ControlPlaneUnavailable("Dagster schedules are unavailable")
    rows_value = result.get("results")
    if not isinstance(rows_value, list):
        raise ControlPlaneUnavailable("Dagster schedules response is invalid")
    rows = cast(list[object], rows_value)
    parsed: dict[str, Mapping[str, object]] = {}
    for raw in rows:
        row = _mapping(raw, "schedule")
        name = row.get("name")
        if not isinstance(name, str):
            raise ControlPlaneUnavailable("Dagster schedule name is invalid")
        parsed[name] = row
    missing = _SCHEDULES.keys() - parsed.keys()
    if missing:
        raise ControlPlaneUnavailable("Dagster did not return every allow-listed schedule")
    return parsed


def _schedule_view(
    rows: Mapping[str, Mapping[str, object]], definition: ControlDefinition
) -> ScheduleView:
    row = rows[definition.schedule_name]
    state = _mapping(row.get("scheduleState"), "schedule state")
    tick = state.get("nextTick")
    timestamp: float | None = None
    if tick is not None:
        raw_timestamp = _mapping(tick, "next tick").get("timestamp")
        if not isinstance(raw_timestamp, (int, float)):
            raise ControlPlaneUnavailable("Dagster next tick is invalid")
        timestamp = float(raw_timestamp)
    return ScheduleView(
        definition=definition,
        status=_schedule_status(row),
        next_tick=datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None,
    )


def _schedule_status(row: Mapping[str, object]) -> ScheduleStatus:
    state = _mapping(row.get("scheduleState"), "schedule state")
    status = state.get("status")
    try:
        return ScheduleStatus(status)
    except (TypeError, ValueError) as error:
        raise ControlPlaneUnavailable("Dagster schedule status is invalid") from error


def _matching_runs(execute: GraphQLTransport, digest: str) -> tuple[tuple[str, str], ...]:
    data = execute(
        _RUNS_QUERY,
        {
            "filter": {
                "tags": [{"key": OWNER_HOME_CORRELATION_TAG, "value": digest}],
            }
        },
    )
    result = _mapping(data.get("runsOrError"), "runsOrError")
    if result.get("__typename") != "Runs":
        raise ControlPlaneUnavailable("Dagster runs are unavailable")
    rows_value = result.get("results")
    if not isinstance(rows_value, list):
        raise ControlPlaneUnavailable("Dagster runs response is invalid")
    rows = cast(list[object], rows_value)
    runs: list[tuple[str, str]] = []
    for raw in rows:
        row = _mapping(raw, "run")
        run_id = row.get("runId")
        returned_job = row.get("jobName")
        if not isinstance(run_id, str) or not isinstance(returned_job, str):
            raise ControlPlaneUnavailable("Dagster run response is invalid")
        runs.append((run_id, returned_job))
    return tuple(runs)


def _existing_run_result(
    matches: tuple[tuple[str, str], ...], requested_job: str
) -> RunStarted | ControlConflict | None:
    if len(matches) > 1:
        return ControlConflict("Multiple Dagster runs share this request correlation")
    if not matches:
        return None
    run_id, existing_job = matches[0]
    if existing_job != requested_job:
        return ControlConflict("This request key belongs to another Dagster job")
    return RunStarted(run_id=run_id, replayed=True)


def _reconcile_schedule_change(
    execute: GraphQLTransport,
    settings: DagsterControlSettings,
    command: ScheduleChangeCommand,
    mutation_error: str | None,
) -> ScheduleChangeResult:
    try:
        row = _read_schedule_rows(execute, settings).get(command.schedule_name)
        if row is None:
            return ControlError("Dagster did not return the changed schedule")
        observed = _schedule_status(row)
    except ControlPlaneUnavailable as error:
        reason = mutation_error or "Dagster schedule change could not be verified"
        return ControlError(f"{reason}; {error}")
    if observed is command.desired_state:
        return ScheduleChanged(status=observed, replayed=mutation_error is not None)
    if mutation_error is not None:
        return ControlError(mutation_error)
    return ControlError(
        f"Dagster reported {observed.value} after requesting {command.desired_state.value}"
    )


def _mutate_schedule(
    execute: GraphQLTransport,
    settings: DagsterControlSettings,
    row: Mapping[str, object],
    desired: ScheduleStatus,
) -> ScheduleStatus:
    if desired is ScheduleStatus.RUNNING:
        schedule_name = row.get("name")
        if not isinstance(schedule_name, str) or not schedule_name:
            raise ControlPlaneUnavailable("Dagster schedule name is invalid")
        variables: Mapping[str, object] = {
            "selector": _repository_selector(settings) | {"scheduleName": schedule_name}
        }
        field = "startSchedule"
        data = execute(_START_SCHEDULE_MUTATION, variables)
    else:
        schedule_id = row.get("id")
        if not isinstance(schedule_id, str) or not schedule_id:
            raise ControlPlaneUnavailable("Dagster schedule identifier is invalid")
        field = "stopRunningSchedule"
        data = execute(_STOP_SCHEDULE_MUTATION, {"id": schedule_id})
    result = _mapping(data.get(field), field)
    if result.get("__typename") != "ScheduleStateResult":
        raise ControlPlaneUnavailable("Dagster rejected the schedule change")
    return _schedule_status(result)


def _correlation_digest(command: RunNowCommand) -> str:
    material = "\0".join((command.idempotency_key, command.actor, OWNER_HOME_SOURCE))
    return hashlib.sha256(material.encode()).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ControlPlaneUnavailable(f"Dagster {label} response is invalid")
    return cast(Mapping[str, object], value)
