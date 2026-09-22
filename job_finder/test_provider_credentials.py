from __future__ import annotations

import base64

import pytest
from pydantic import SecretStr

from job_finder.provider_credentials import ProviderKind, credential_cipher


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
