from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import pytest
from pydantic import SecretStr

from job_finder.provider_credentials import (
    REQUIRED_CAPABILITIES,
    ProviderKind,
    credential_cipher,
    production_provider_validators,
)


@dataclass(frozen=True)
class _Response:
    status_code: int
    text: str


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
def test_production_provider_validators_accept_runtime_response_models(
    provider: ProviderKind,
    responses: tuple[dict[str, object], ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = list(responses)

    def post(*_args: object, **_kwargs: object) -> _Response:
        return _Response(status_code=200, text=json.dumps(pending.pop(0)))

    monkeypatch.setattr("job_finder.provider_credentials.requests.post", post)

    result = production_provider_validators()[provider](SecretStr("valid-secret"))

    assert result.capabilities == REQUIRED_CAPABILITIES[provider]


@pytest.mark.parametrize("provider", tuple(ProviderKind))
def test_production_provider_validators_reject_success_with_the_wrong_shape(
    provider: ProviderKind,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def post(*_args: object, **_kwargs: object) -> _Response:
        return _Response(status_code=200, text='{"unexpected": true}')

    monkeypatch.setattr(
        "job_finder.provider_credentials.requests.post",
        post,
    )

    result = production_provider_validators()[provider](SecretStr("valid-secret"))

    assert result.capabilities == ()
    assert not result.invalid_credentials
