from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast
from uuid import UUID

import pytest
import requests

from job_finder.config import DagsterControlSettings
from job_finder.review.control_plane import (
    OWNER_HOME_ACTOR_TAG,
    OWNER_HOME_CORRELATION_TAG,
    OWNER_HOME_SOURCE_TAG,
    ControlConflict,
    RunNowCommand,
    RunStarted,
    ScheduleChangeCommand,
    ScheduleChanged,
    ScheduleStateConflict,
    ScheduleStatus,
    dagster_control_plane_service,
)

_LOCATION_PATH = Path(__file__).with_name("control_plane_location.py")
_LOCATION_NAME = "control_plane_contract"
_NOW = datetime(2026, 9, 22, 2, tzinfo=UTC)

_DAGSTER_YAML = """\
storage:
  sqlite:
    base_dir: {storage_dir}
run_launcher:
  module: dagster._core.launcher.default_run_launcher
  class: DefaultRunLauncher
scheduler:
  module: dagster._core.scheduler.scheduler
  class: DagsterDaemonScheduler
"""

_WORKSPACE_YAML = """\
load_from:
  - python_file:
      relative_path: {location_path}
      attribute: defs
      location_name: {location_name}
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        address = cast(tuple[object, ...], probe.getsockname())
        port = address[1]
        assert isinstance(port, int)
        return port


def _run_now_command(*, job_name: str = "job_finder") -> RunNowCommand:
    return RunNowCommand(
        job_name=job_name, idempotency_key="private-contract-key", actor="owner", timestamp=_NOW
    )


def _schedule_command(expected: ScheduleStatus, desired: ScheduleStatus) -> ScheduleChangeCommand:
    return ScheduleChangeCommand(
        schedule_name="job_finder_schedule",
        expected_state=expected,
        desired_state=desired,
        actor="owner",
        timestamp=_NOW,
    )


@pytest.fixture(scope="module")
def control_plane_settings(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DagsterControlSettings]:
    home = tmp_path_factory.mktemp("dagster_home")
    storage_dir = tmp_path_factory.mktemp("dagster_storage")
    (home / "dagster.yaml").write_text(_DAGSTER_YAML.format(storage_dir=storage_dir))
    (home / "workspace.yaml").write_text(
        _WORKSPACE_YAML.format(location_path=_LOCATION_PATH, location_name=_LOCATION_NAME)
    )
    port = _free_port()
    webserver_log = (home / "webserver.log").open("w")
    webserver = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "dagster-webserver"),
            "-w",
            str(home / "workspace.yaml"),
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
        ],
        env={**os.environ, "DAGSTER_HOME": str(home)},
        stdout=webserver_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        graphql_url = _wait_until_ready(webserver, port, webserver_log)
        yield DagsterControlSettings.model_validate(
            {
                "graphql_url": graphql_url,
                "repository_location_name": _LOCATION_NAME,
                "repository_name": "__repository__",
                "timeout_seconds": 30,
            }
        )
    finally:
        if webserver.poll() is None:
            os.killpg(os.getpgid(webserver.pid), signal.SIGTERM)
            try:
                webserver.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(webserver.pid), signal.SIGKILL)
                webserver.wait(timeout=15)
        webserver_log.close()


def _wait_until_ready(webserver: subprocess.Popen[bytes], port: int, log: TextIO) -> str:
    graphql_url = f"http://127.0.0.1:{port}/graphql"
    deadline = time.monotonic() + 120
    while True:
        if webserver.poll() is not None:
            log.flush()
            raise AssertionError(f"dagster-webserver exited during startup:\n{log.read()[:4000]}")
        try:
            response = requests.post(
                graphql_url, json={"query": "{ repositoriesOrError { __typename } }"}, timeout=5
            )
            payload = cast(object, response.json())
            if (
                response.status_code == 200
                and isinstance(payload, dict)
                and cast(Mapping[str, object], payload).get("data") is not None
            ):
                return graphql_url
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            raise AssertionError("dagster-webserver did not become ready within 120s")
        time.sleep(1)


def _run_tags(settings: DagsterControlSettings, run_id: str) -> Mapping[str, str]:
    response = requests.post(
        str(settings.graphql_url),
        json={
            "query": (
                '{ runOrError(runId: "'
                + run_id
                + '") { __typename ... on Run { tags { key value } } } }'
            )
        },
        timeout=30,
    )
    payload = cast(object, response.json())
    assert isinstance(payload, dict)
    data = cast(Mapping[str, object], payload).get("data")
    assert isinstance(data, dict)
    run = cast(Mapping[str, object], data).get("runOrError")
    assert isinstance(run, dict)
    tags = cast(Mapping[str, object], run).get("tags")
    assert isinstance(tags, list)
    result: dict[str, str] = {}
    for raw_tag in cast(list[object], tags):
        assert isinstance(raw_tag, dict)
        tag = cast(Mapping[str, object], raw_tag)
        key = tag.get("key")
        value = tag.get("value")
        assert isinstance(key, str)
        assert isinstance(value, str)
        result[key] = value
    return result


def test_run_now_launches_a_real_dagster_run_with_owner_provenance(
    control_plane_settings: DagsterControlSettings,
) -> None:
    service = dagster_control_plane_service(control_plane_settings)

    snapshot = service.load()
    first = service.run_now(_run_now_command())
    replayed = service.run_now(_run_now_command())
    conflict = service.run_now(_run_now_command(job_name="job_work_queue"))

    assert {view.definition.schedule_name for view in snapshot.schedules} == {
        "job_finder_schedule",
        "job_work_queue_schedule",
        "review_sample_schedule",
        "langfuse_projection_schedule",
    }
    assert all(view.status is ScheduleStatus.RUNNING for view in snapshot.schedules)
    assert all(view.next_tick is not None for view in snapshot.schedules)
    assert isinstance(first, RunStarted)
    UUID(first.run_id)
    assert isinstance(replayed, RunStarted)
    assert replayed.run_id == first.run_id
    assert replayed.replayed is True
    assert isinstance(conflict, ControlConflict)

    tags = _run_tags(control_plane_settings, first.run_id)
    assert re.fullmatch(r"[0-9a-f]{64}", tags[OWNER_HOME_CORRELATION_TAG])
    assert "private-contract-key" not in tags.values()
    assert tags[OWNER_HOME_ACTOR_TAG] == "owner"
    assert tags[OWNER_HOME_SOURCE_TAG] == "owner-home"


def test_schedule_changes_transition_real_dagster_schedule_state(
    control_plane_settings: DagsterControlSettings,
) -> None:
    service = dagster_control_plane_service(control_plane_settings)

    stopped = service.change_schedule(
        _schedule_command(ScheduleStatus.RUNNING, ScheduleStatus.STOPPED)
    )
    stale = service.change_schedule(
        _schedule_command(ScheduleStatus.RUNNING, ScheduleStatus.STOPPED)
    )
    resumed = service.change_schedule(
        _schedule_command(ScheduleStatus.STOPPED, ScheduleStatus.RUNNING)
    )
    already_running = service.change_schedule(
        _schedule_command(ScheduleStatus.RUNNING, ScheduleStatus.RUNNING)
    )

    assert stopped == ScheduleChanged(status=ScheduleStatus.STOPPED, replayed=False)
    assert stale == ScheduleStateConflict(
        expected=ScheduleStatus.RUNNING, observed=ScheduleStatus.STOPPED
    )
    assert resumed == ScheduleChanged(status=ScheduleStatus.RUNNING, replayed=False)
    assert already_running == ScheduleChanged(status=ScheduleStatus.RUNNING, replayed=True)
    assert [
        view.status
        for view in service.load().schedules
        if view.definition.schedule_name == "job_finder_schedule"
    ] == [ScheduleStatus.RUNNING]
