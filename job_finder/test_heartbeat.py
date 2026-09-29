from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, final, override

import requests
from pytest import LogCaptureFixture

from job_finder.dagster import ping_heartbeat


class _FailingSender:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> None:
        self.calls.append(url)
        raise requests.ConnectionError("down")


@final
class _StatusServer(ThreadingHTTPServer):
    status: int
    requests_received: list[tuple[str, str]]

    def __init__(self, status: int) -> None:
        self.status = status
        self.requests_received = []
        super().__init__(("127.0.0.1", 0), _StatusHandler)


@final
class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(_StatusServer, self.server)
        server.requests_received.append((self.command, self.path))
        self.send_response(server.status)
        self.send_header("content-length", "0")
        self.end_headers()

    @override
    def log_message(self, format: str, *_args: object) -> None:
        pass


@contextmanager
def _http_status(status: int) -> Generator[_StatusServer]:
    server = _StatusServer(status)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _url(server: _StatusServer, token: str) -> str:
    _host, port = cast(tuple[str, int], server.server_address)
    return f"http://127.0.0.1:{port}/{token}"


def test_pings_the_heartbeat_url() -> None:
    with _http_status(200) as server:
        ping_heartbeat(_url(server, "secret-token"))

    assert server.requests_received == [("GET", "/secret-token")]


def test_skips_a_missing_heartbeat_url(caplog: LogCaptureFixture) -> None:
    with _http_status(200) as server:
        ping_heartbeat(None)

    assert server.requests_received == []
    assert "Heartbeat ping failed" not in caplog.text


def test_a_failed_ping_warns_without_exposing_the_url(caplog: LogCaptureFixture) -> None:
    failing = _FailingSender()

    ping_heartbeat("https://heartbeat.example/secret-token", sender=failing)

    assert "Heartbeat ping failed: ConnectionError" in caplog.text
    assert "secret-token" not in caplog.text


def test_http_error_response_warns_without_interrupting_run(
    caplog: LogCaptureFixture,
) -> None:
    with _http_status(503) as server:
        ping_heartbeat(_url(server, "secret-token"))

    assert "Heartbeat ping failed: HTTPError" in caplog.text
    assert "secret-token" not in caplog.text


def test_a_successful_default_ping_emits_no_warning(caplog: LogCaptureFixture) -> None:
    with _http_status(200) as server:
        ping_heartbeat(_url(server, "secret-token"))

    assert "Heartbeat ping failed" not in caplog.text
    assert "secret-token" not in caplog.text
