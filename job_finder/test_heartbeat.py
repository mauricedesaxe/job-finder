from __future__ import annotations

import requests
from pytest import LogCaptureFixture, MonkeyPatch

from job_finder.dagster import ping_heartbeat


class _FailingSender:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> None:
        self.calls.append(url)
        raise requests.ConnectionError("down")


def test_pings_the_heartbeat_url() -> None:
    calls: list[str] = []

    def sender(url: str) -> None:
        calls.append(url)

    ping_heartbeat("https://heartbeat.example/ping", sender=sender)

    assert calls == ["https://heartbeat.example/ping"]


def test_skips_a_missing_heartbeat_url() -> None:
    calls: list[str] = []

    def sender(url: str) -> None:
        calls.append(url)

    ping_heartbeat(None, sender=sender)

    assert calls == []


def test_a_failed_ping_warns_without_exposing_the_url(caplog: LogCaptureFixture) -> None:
    failing = _FailingSender()

    ping_heartbeat("https://heartbeat.example/secret-token", sender=failing)

    assert failing.calls == ["https://heartbeat.example/secret-token"]
    assert "Heartbeat ping failed: ConnectionError" in caplog.text
    assert "secret-token" not in caplog.text


def test_http_error_response_warns_without_interrupting_run(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    response = requests.Response()
    response.status_code = 503
    response.url = "https://heartbeat.example/secret-token"

    def get(_url: str, timeout: int) -> requests.Response:
        assert timeout == 5
        return response

    monkeypatch.setattr("job_finder.dagster.requests.get", get)

    ping_heartbeat(response.url)

    assert "Heartbeat ping failed: HTTPError" in caplog.text
    assert "secret-token" not in caplog.text
