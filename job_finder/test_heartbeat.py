from __future__ import annotations

import requests

from job_finder.dagster import _ping_heartbeat


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

    _ping_heartbeat("https://heartbeat.example/ping", sender=sender)

    assert calls == ["https://heartbeat.example/ping"]


def test_skips_a_missing_heartbeat_url() -> None:
    calls: list[str] = []

    def sender(url: str) -> None:
        calls.append(url)

    _ping_heartbeat(None, sender=sender)

    assert calls == []


def test_a_failed_ping_never_raises() -> None:
    failing = _FailingSender()

    _ping_heartbeat("https://heartbeat.example/ping", sender=failing)

    assert failing.calls == ["https://heartbeat.example/ping"]
