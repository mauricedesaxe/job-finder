from __future__ import annotations

import base64
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import cast, final, override

import pytest
from pydantic import SecretStr

from job_finder.provider_credentials import (
    REQUIRED_CAPABILITIES,
    ProviderKind,
    credential_cipher,
    production_provider_validators,
)


@final
class _StubServer(ThreadingHTTPServer):
    responses: list[tuple[int, str]]
    requests: list[tuple[str, dict[str, str], bytes]]

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = list(responses)
        self.requests = []
        super().__init__(("127.0.0.1", 0), _RecordingHandler)


@final
class _RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        stub = cast(_StubServer, self.server)
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        stub.requests.append((self.path, dict(self.headers), body))
        status, text = stub.responses.pop(0)
        payload = text.encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @override
    def log_message(self, format: str, *_args: object) -> None:
        pass


@contextmanager
def _provider_stub(responses: list[tuple[int, str]]) -> Generator[_StubServer]:
    server = _StubServer(responses)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _stub_urls(server: _StubServer) -> dict[str, str]:
    _host, port = cast(tuple[str, int], server.server_address)
    base = f"http://127.0.0.1:{port}"
    return {
        "jina_search_url": f"{base}/search",
        "jina_reader_url": f"{base}/reader",
        "openrouter_url": f"{base}/openrouter",
        "typesafe_url": f"{base}/typesafe",
    }


_VALIDATION_RESPONSES: tuple[tuple[ProviderKind, tuple[dict[str, object], ...]], ...] = (
    (
        ProviderKind.JINA,
        (
            {"code": 200, "data": []},
            {"code": 200, "data": {"title": "Example", "content": "Readable"}},
        ),
    ),
    (
        ProviderKind.OPENROUTER,
        (
            {
                "id": "validation",
                "model": "google/gemini-2.5-flash-lite",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "validate_provider",
                                        "arguments": '{"ready":true}',
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0.001"},
            },
        ),
    ),
    (
        ProviderKind.TYPESAFE,
        (
            {
                "model": "jev-1.13.0",
                "answers": {"validation": {"type": "noul", "noul": 1}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ),
)

_EXPECTED_PATHS: dict[ProviderKind, list[str]] = {
    ProviderKind.JINA: ["/search", "/reader"],
    ProviderKind.OPENROUTER: ["/openrouter"],
    ProviderKind.TYPESAFE: ["/typesafe"],
}


def test_provider_credentials_are_authenticated_and_bound_to_provider_generation() -> None:
    documented_key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    cipher = credential_cipher(SecretStr(documented_key))
    secret = SecretStr("provider-secret-that-must-not-leak")

    first_nonce, first_ciphertext = cipher.encrypt(ProviderKind.JINA, 1, secret)
    second_nonce, second_ciphertext = cipher.encrypt(ProviderKind.JINA, 1, secret)

    assert first_nonce != second_nonce
    assert first_ciphertext != second_ciphertext
    assert secret.get_secret_value().encode() not in first_ciphertext
    assert (
        cipher.decrypt(ProviderKind.JINA, 1, first_nonce, first_ciphertext).get_secret_value()
        == secret.get_secret_value()
    )
    with pytest.raises(ValueError, match="could not be decrypted"):
        cipher.decrypt(ProviderKind.OPENROUTER, 1, first_nonce, first_ciphertext)
    with pytest.raises(ValueError, match="could not be decrypted"):
        cipher.decrypt(ProviderKind.JINA, 2, first_nonce, first_ciphertext)


@pytest.mark.parametrize("encoded", ["not-base64", base64.urlsafe_b64encode(b"short").decode()])
def test_provider_credential_key_requires_32_base64_bytes(encoded: str) -> None:
    with pytest.raises(ValueError, match="credential encryption key"):
        credential_cipher(SecretStr(encoded))


@pytest.mark.parametrize(
    ("provider", "responses"),
    _VALIDATION_RESPONSES,
)
def test_production_provider_validators_authenticate_and_validate_over_http(
    provider: ProviderKind,
    responses: tuple[dict[str, object], ...],
) -> None:
    with _provider_stub([(200, json.dumps(body)) for body in responses]) as server:
        validators = production_provider_validators(**_stub_urls(server))

        result = validators[provider](SecretStr("valid-secret"))

    assert result.capabilities == REQUIRED_CAPABILITIES[provider]
    assert not result.invalid_credentials
    assert [path for path, _headers, _body in server.requests] == _EXPECTED_PATHS[provider]
    request_bodies = {path: json.loads(body) for path, _headers, body in server.requests}
    for _path, headers, _body in server.requests:
        assert headers["authorization"] == "Bearer valid-secret"
    if provider is ProviderKind.JINA:
        assert request_bodies == {
            "/search": {"q": "site:boards.greenhouse.io software engineer"},
            "/reader": {"url": "https://example.com"},
        }
    elif provider is ProviderKind.OPENROUTER:
        assert request_bodies["/openrouter"]["model"] == "google/gemini-2.5-flash-lite"
        assert request_bodies["/openrouter"]["messages"] == [
            {"role": "user", "content": 'Return {"ready":true}.'}
        ]
        assert request_bodies["/openrouter"]["max_tokens"] == 16
        assert request_bodies["/openrouter"]["usage"] == {"include": True}
        tools = cast(list[dict[str, object]], request_bodies["/openrouter"]["tools"])
        assert [cast(dict[str, str], tool["function"])["name"] for tool in tools] == [
            "validate_provider"
        ]
        assert request_bodies["/openrouter"]["tool_choice"] == {
            "type": "function",
            "function": {"name": "validate_provider"},
        }
    else:
        assert request_bodies["/typesafe"]["state"] == "Provider capability validation."
        assert request_bodies["/typesafe"]["model"] == "jev-1.13.0"
        questions = cast(dict[str, object], request_bodies["/typesafe"]["questions"])
        assert set(questions) == {"validation"}


@pytest.mark.parametrize("provider", tuple(ProviderKind))
def test_production_provider_validators_reject_success_with_the_wrong_shape(
    provider: ProviderKind,
) -> None:
    with _provider_stub([(200, '{"unexpected": true}')]) as server:
        validators = production_provider_validators(**_stub_urls(server))

        result = validators[provider](SecretStr("valid-secret"))

    assert result.capabilities == ()
    assert not result.invalid_credentials


@pytest.mark.parametrize("provider", tuple(ProviderKind))
@pytest.mark.parametrize("status", (401, 403))
def test_provider_auth_failures_map_to_invalid_credentials(
    provider: ProviderKind, status: int
) -> None:
    with _provider_stub([(status, '{"error": "unauthorized"}')]) as server:
        validators = production_provider_validators(**_stub_urls(server))

        result = validators[provider](SecretStr("valid-secret"))

    assert result.capabilities == ()
    assert result.invalid_credentials


@pytest.mark.parametrize("provider", tuple(ProviderKind))
def test_provider_server_errors_map_to_temporarily_unavailable(provider: ProviderKind) -> None:
    with _provider_stub([(500, "internal server error")]) as server:
        validators = production_provider_validators(**_stub_urls(server))

        result = validators[provider](SecretStr("valid-secret"))

    assert result.capabilities == ()
    assert not result.invalid_credentials


@pytest.mark.parametrize("provider", tuple(ProviderKind))
def test_provider_non_json_responses_map_to_no_capabilities(provider: ProviderKind) -> None:
    with _provider_stub([(200, "not-json")]) as server:
        validators = production_provider_validators(**_stub_urls(server))

        result = validators[provider](SecretStr("valid-secret"))

    assert result.capabilities == ()
    assert not result.invalid_credentials


@pytest.mark.parametrize("provider", tuple(ProviderKind))
def test_provider_connection_failures_map_to_no_capabilities(provider: ProviderKind) -> None:
    with socket() as probe:
        probe.bind(("127.0.0.1", 0))
        _host, port = cast(tuple[str, int], probe.getsockname())
        base = f"http://127.0.0.1:{port}"
        validators = production_provider_validators(
            jina_search_url=f"{base}/search",
            jina_reader_url=f"{base}/reader",
            openrouter_url=f"{base}/openrouter",
            typesafe_url=f"{base}/typesafe",
        )

        result = validators[provider](SecretStr("valid-secret"))

    assert result.capabilities == ()
    assert not result.invalid_credentials
