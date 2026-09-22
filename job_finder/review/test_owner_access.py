from __future__ import annotations

import pytest

from job_finder.review.owner_access import hash_owner_password, verify_owner_password


def test_owner_password_hash_is_salted_and_verifiable() -> None:
    first = hash_owner_password("a-secure-owner-password")
    second = hash_owner_password("a-secure-owner-password")

    assert first != second
    assert "a-secure-owner-password" not in first
    assert verify_owner_password("a-secure-owner-password", first) is True
    assert verify_owner_password("another-password", first) is False


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "scrypt$v=2$n=16384$r=8$p=1$bad$bad",
        "scrypt$v=1$n=999999$r=8$p=1$bad$bad",
        "scrypt$v=1$n=16384$r=8$p=1$bad",
    ],
)
def test_owner_password_verification_rejects_malformed_hashes(encoded: str) -> None:
    assert verify_owner_password("a-secure-owner-password", encoded) is False


def test_owner_password_policy_is_bounded() -> None:
    with pytest.raises(ValueError, match="12 to 1024"):
        hash_owner_password("too-short")
    with pytest.raises(ValueError, match="12 to 1024"):
        hash_owner_password("x" * 1025)


def test_owner_password_verification_rejects_oversized_input_before_hashing() -> None:
    encoded = hash_owner_password("a-secure-owner-password")

    assert verify_owner_password("x" * (64 * 1024 + 1), encoded) is False
