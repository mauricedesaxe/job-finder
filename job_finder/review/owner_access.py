from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from job_finder.review.postgres import Connection, ConnectionFactory

_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAX_MEMORY = 256 * 1024 * 1024
_SALT_BYTES = 16
_HASH_BYTES = 32
MINIMUM_PASSWORD_LENGTH = 12
MAXIMUM_PASSWORD_LENGTH = 1024
MAXIMUM_PASSWORD_INPUT_LENGTH = 64 * 1024


class OnboardingStage(StrEnum):
    LEGACY_OWNER_IMPORT = "legacy_owner_import"
    OWNER_ACCOUNT = "owner_account"
    PROVIDERS = "providers"
    PREFERENCES = "preferences"
    BUDGET = "budget"
    TEST_SEARCH = "test_search"
    COMPLETE = "complete"


@dataclass(frozen=True)
class OwnerAccessState:
    stage: OnboardingStage
    has_password: bool


@dataclass(frozen=True)
class OwnerBootstrapped:
    state: OwnerAccessState


@dataclass(frozen=True)
class OwnerBootstrapConflict:
    state: OwnerAccessState


OwnerBootstrapResult: TypeAlias = OwnerBootstrapped | OwnerBootstrapConflict


@dataclass(frozen=True)
class OwnerAccessService:
    load_state: Callable[[], OwnerAccessState]
    authenticate: Callable[[str], bool]
    bootstrap: Callable[[str], OwnerBootstrapResult]


def hash_owner_password(password: str, *, salt: bytes | None = None) -> str:
    return _hash_owner_password(password, salt=salt, enforce_maximum=True)


def _hash_owner_password(password: str, *, salt: bytes | None = None, enforce_maximum: bool) -> str:
    _validate_password(password, enforce_maximum=enforce_maximum)
    password_salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    if len(password_salt) != _SALT_BYTES:
        raise ValueError("password salt must contain 16 bytes")
    derived = hashlib.scrypt(
        password.encode(),
        salt=password_salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAX_MEMORY,
        dklen=_HASH_BYTES,
    )
    encoded_salt = base64.urlsafe_b64encode(password_salt).decode()
    encoded_hash = base64.urlsafe_b64encode(derived).decode()
    return f"scrypt$v=1$n={_SCRYPT_N}$r={_SCRYPT_R}$p={_SCRYPT_P}" f"${encoded_salt}${encoded_hash}"


def verify_owner_password(password: str, encoded: str) -> bool:
    try:
        _validate_password(password, enforce_maximum=False)
        parts = encoded.split("$")
        if len(parts) != 7 or parts[:5] != [
            "scrypt",
            "v=1",
            f"n={_SCRYPT_N}",
            f"r={_SCRYPT_R}",
            f"p={_SCRYPT_P}",
        ]:
            return False
        salt = base64.b64decode(parts[5], altchars=b"-_", validate=True)
        expected = base64.b64decode(parts[6], altchars=b"-_", validate=True)
        if len(salt) != _SALT_BYTES or len(expected) != _HASH_BYTES:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            maxmem=_SCRYPT_MAX_MEMORY,
            dklen=_HASH_BYTES,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def postgres_owner_access_service(connect: ConnectionFactory) -> OwnerAccessService:
    def load_state() -> OwnerAccessState:
        with connect() as connection:
            return _load_state(connection)

    def authenticate(password: str) -> bool:
        with connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM owner_onboarding WHERE singleton_id = 1"
            ).fetchone()
        return (
            row is not None and row[0] is not None and verify_owner_password(password, str(row[0]))
        )

    def bootstrap(password: str) -> OwnerBootstrapResult:
        encoded = hash_owner_password(password)
        with connect() as connection, connection.transaction():
            changed = connection.execute(
                """
                UPDATE owner_onboarding
                SET stage = 'providers', password_hash = %s, updated_at = CURRENT_TIMESTAMP
                WHERE singleton_id = 1 AND stage = 'owner_account' AND password_hash IS NULL
                """,
                (encoded,),
            ).rowcount
            state = _load_state(connection)
        if changed == 1:
            return OwnerBootstrapped(state)
        return OwnerBootstrapConflict(state)

    return OwnerAccessService(load_state=load_state, authenticate=authenticate, bootstrap=bootstrap)


def import_legacy_owner_password(connection: Connection, password: str | None) -> OwnerAccessState:
    state = _load_state(connection)
    if state.stage is not OnboardingStage.LEGACY_OWNER_IMPORT or password is None:
        return state
    encoded = _hash_owner_password(password, enforce_maximum=False)
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'complete', password_hash = %s, updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1 AND stage = 'legacy_owner_import' AND password_hash IS NULL
            """,
            (encoded,),
        )
        return _load_state(connection)


def _load_state(connection: Connection) -> OwnerAccessState:
    row = connection.execute(
        "SELECT stage, password_hash IS NOT NULL FROM owner_onboarding WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Owner onboarding state is missing")
    return OwnerAccessState(stage=OnboardingStage(str(row[0])), has_password=bool(row[1]))


def _validate_password(password: str, *, enforce_maximum: bool) -> None:
    if len(password) > MAXIMUM_PASSWORD_INPUT_LENGTH:
        raise ValueError("password exceeds the safe input limit")
    if len(password) < MINIMUM_PASSWORD_LENGTH or (
        enforce_maximum and len(password) > MAXIMUM_PASSWORD_LENGTH
    ):
        raise ValueError("password must contain 12 to 1024 characters")
