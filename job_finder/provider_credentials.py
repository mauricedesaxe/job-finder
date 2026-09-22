from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal, TypeAlias, cast

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from job_finder.discovery.jina import JinaReaderEnvelope, JinaSearchEnvelope
from job_finder.evaluation.jev import JevSystemOneResponse
from job_finder.evaluation.openrouter import OpenRouterCompletion
from job_finder.review.owner_access import OnboardingStage, OwnerAccessState
from job_finder.review.postgres import Connection, ConnectionFactory


class ProviderKind(StrEnum):
    JINA = "jina"
    OPENROUTER = "openrouter"
    TYPESAFE = "typesafe"


class ProviderCapability(StrEnum):
    SEARCH = "search"
    SCRAPE = "scrape"
    STRUCTURED_GENERATION = "structured_generation"
    USAGE_COST = "usage_cost"
    RELEVANCE_EVALUATION = "relevance_evaluation"


REQUIRED_CAPABILITIES: Mapping[ProviderKind, tuple[ProviderCapability, ...]] = {
    ProviderKind.JINA: (ProviderCapability.SEARCH, ProviderCapability.SCRAPE),
    ProviderKind.OPENROUTER: (
        ProviderCapability.STRUCTURED_GENERATION,
        ProviderCapability.USAGE_COST,
    ),
    ProviderKind.TYPESAFE: (
        ProviderCapability.RELEVANCE_EVALUATION,
        ProviderCapability.USAGE_COST,
    ),
}


class ProviderCredentialModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ProviderCredentialState(ProviderCredentialModel):
    provider: ProviderKind
    generation: int = Field(ge=0)
    capabilities: tuple[ProviderCapability, ...] = ()
    validated_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return self.generation > 0


class ProviderSetupSnapshot(ProviderCredentialModel):
    credentials: tuple[ProviderCredentialState, ...]

    @property
    def ready(self) -> bool:
        return all(
            credential.capabilities == REQUIRED_CAPABILITIES[credential.provider]
            for credential in self.credentials
        )


class ProviderCredentialStored(ProviderCredentialModel):
    kind: Literal["stored"] = "stored"
    state: ProviderCredentialState


class ProviderCredentialRejected(ProviderCredentialModel):
    kind: Literal["rejected"] = "rejected"
    error_code: Literal["invalid_credentials", "temporarily_unavailable"]


class ProviderCredentialChanged(ProviderCredentialModel):
    kind: Literal["changed"] = "changed"
    current: ProviderCredentialState


ProviderCredentialResult: TypeAlias = (
    ProviderCredentialStored | ProviderCredentialRejected | ProviderCredentialChanged
)


class ProviderStageAdvanced(ProviderCredentialModel):
    kind: Literal["advanced"] = "advanced"
    state: OwnerAccessState


class ProviderStageBlocked(ProviderCredentialModel):
    kind: Literal["blocked"] = "blocked"
    state: OwnerAccessState


ProviderStageResult: TypeAlias = ProviderStageAdvanced | ProviderStageBlocked


class ProviderValidation(ProviderCredentialModel):
    capabilities: tuple[ProviderCapability, ...] = ()
    invalid_credentials: bool = False


ProviderValidator = Callable[[SecretStr], ProviderValidation]


@dataclass(frozen=True)
class CredentialCipher:
    encrypt: Callable[[ProviderKind, int, SecretStr], tuple[bytes, bytes]]
    decrypt: Callable[[ProviderKind, int, bytes, bytes], SecretStr]


@dataclass(frozen=True)
class ProviderSetupService:
    inspect: Callable[[], ProviderSetupSnapshot]
    replace: Callable[[ProviderKind, SecretStr, int, str, datetime], ProviderCredentialResult]
    advance: Callable[[], ProviderStageResult]
    resolve: Callable[[ProviderKind], SecretStr]


class ExecutionProviderCredentials(ProviderCredentialModel):
    jina: SecretStr
    openrouter: SecretStr
    typesafe: SecretStr


class _OpenRouterValidationOutput(ProviderCredentialModel):
    ready: Literal[True]


def credential_cipher(encoded_key: SecretStr) -> CredentialCipher:
    try:
        encoded = encoded_key.get_secret_value()
        key = base64.urlsafe_b64decode((encoded + "=" * (-len(encoded) % 4)).encode())
    except (ValueError, binascii.Error) as error:
        raise ValueError("credential encryption key must be URL-safe base64") from error
    if len(key) != 32:
        raise ValueError("credential encryption key must encode exactly 32 bytes")
    aes = AESGCM(key)

    def associated_data(provider: ProviderKind, generation: int) -> bytes:
        return f"job-finder/provider-credential/v1/{provider.value}/{generation}".encode()

    def encrypt(provider: ProviderKind, generation: int, value: SecretStr) -> tuple[bytes, bytes]:
        nonce = secrets.token_bytes(12)
        ciphertext = aes.encrypt(
            nonce,
            value.get_secret_value().encode(),
            associated_data(provider, generation),
        )
        return nonce, ciphertext

    def decrypt(
        provider: ProviderKind, generation: int, nonce: bytes, ciphertext: bytes
    ) -> SecretStr:
        try:
            plaintext = aes.decrypt(
                nonce,
                ciphertext,
                associated_data(provider, generation),
            )
        except InvalidTag as error:
            raise ValueError("provider credential could not be decrypted") from error
        return SecretStr(plaintext.decode())

    return CredentialCipher(encrypt=encrypt, decrypt=decrypt)


def postgres_provider_setup_service(
    connect: ConnectionFactory,
    cipher: CredentialCipher,
    validators: Mapping[ProviderKind, ProviderValidator],
) -> ProviderSetupService:
    def inspect() -> ProviderSetupSnapshot:
        with connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, generation, capabilities, validated_at, nonce, ciphertext
                FROM provider_credentials
                ORDER BY provider
                """
            ).fetchall()
        stored: dict[ProviderKind, ProviderCredentialState] = {}
        for row in rows:
            provider = ProviderKind(str(row[0]))
            generation = cast(int, row[1])
            try:
                _ = cipher.decrypt(
                    provider,
                    generation,
                    cast(bytes, row[4]),
                    cast(bytes, row[5]),
                )
            except ValueError:
                capabilities: tuple[ProviderCapability, ...] = ()
                validated_at = None
            else:
                capabilities = tuple(ProviderCapability(value) for value in cast(list[str], row[2]))
                validated_at = cast(datetime, row[3])
            stored[provider] = ProviderCredentialState(
                provider=provider,
                generation=generation,
                capabilities=capabilities,
                validated_at=validated_at,
            )
        return ProviderSetupSnapshot(
            credentials=tuple(
                stored.get(provider, ProviderCredentialState(provider=provider, generation=0))
                for provider in ProviderKind
            )
        )

    def replace(
        provider: ProviderKind,
        secret: SecretStr,
        expected_generation: int,
        actor: str,
        timestamp: datetime,
    ) -> ProviderCredentialResult:
        with connect() as connection, connection.transaction():
            _ = connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"job-finder-provider-credential:{provider.value}",),
            ).fetchone()
            current_row = connection.execute(
                """
                SELECT generation, capabilities, validated_at
                FROM provider_credentials
                WHERE provider = %s
                """,
                (provider.value,),
            ).fetchone()
            current_generation = 0 if current_row is None else cast(int, current_row[0])
            if current_generation != expected_generation:
                current = (
                    ProviderCredentialState(provider=provider, generation=0)
                    if current_row is None
                    else ProviderCredentialState(
                        provider=provider,
                        generation=current_generation,
                        capabilities=tuple(
                            ProviderCapability(value) for value in cast(list[str], current_row[1])
                        ),
                        validated_at=cast(datetime, current_row[2]),
                    )
                )
                return ProviderCredentialChanged(current=current)
            validation = validators[provider](secret)
            if validation.capabilities != REQUIRED_CAPABILITIES[provider]:
                return ProviderCredentialRejected(
                    error_code=(
                        "invalid_credentials"
                        if validation.invalid_credentials
                        else "temporarily_unavailable"
                    )
                )
            generation = expected_generation + 1
            nonce, ciphertext = cipher.encrypt(provider, generation, secret)
            capabilities = tuple(capability.value for capability in validation.capabilities)
            if current_row is None:
                row = connection.execute(
                    """
                    INSERT INTO provider_credentials (
                      provider, generation, nonce, ciphertext, capabilities,
                      validated_at, updated_at, updated_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING generation, capabilities, validated_at
                    """,
                    (
                        provider.value,
                        generation,
                        nonce,
                        ciphertext,
                        list(capabilities),
                        timestamp,
                        timestamp,
                        actor,
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    UPDATE provider_credentials
                    SET generation = %s, nonce = %s, ciphertext = %s, capabilities = %s,
                        validated_at = %s, updated_at = %s, updated_by = %s
                    WHERE provider = %s AND generation = %s
                    RETURNING generation, capabilities, validated_at
                    """,
                    (
                        generation,
                        nonce,
                        ciphertext,
                        list(capabilities),
                        timestamp,
                        timestamp,
                        actor,
                        provider.value,
                        expected_generation,
                    ),
                ).fetchone()
            if row is None:
                raise RuntimeError("Provider credential replacement lost its lock")
            return ProviderCredentialStored(
                state=ProviderCredentialState(
                    provider=provider,
                    generation=cast(int, row[0]),
                    capabilities=tuple(
                        ProviderCapability(value) for value in cast(list[str], row[1])
                    ),
                    validated_at=cast(datetime, row[2]),
                )
            )

    def advance() -> ProviderStageResult:
        with connect() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT stage, password_hash IS NOT NULL
                FROM owner_onboarding
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("Owner onboarding state is missing")
            state = OwnerAccessState(stage=OnboardingStage(str(row[0])), has_password=bool(row[1]))
            if state.stage is not OnboardingStage.PROVIDERS:
                return ProviderStageBlocked(state=state)
            stored = inspect()
            if not stored.ready:
                return ProviderStageBlocked(state=state)
            _ = connection.execute(
                """
                UPDATE owner_onboarding
                SET stage = 'preferences', updated_at = CURRENT_TIMESTAMP
                WHERE singleton_id = 1 AND stage = 'providers'
                """
            )
        return ProviderStageAdvanced(
            state=OwnerAccessState(stage=OnboardingStage.PREFERENCES, has_password=True)
        )

    def resolve(provider: ProviderKind) -> SecretStr:
        with connect() as connection:
            row = connection.execute(
                """
                SELECT generation, nonce, ciphertext
                FROM provider_credentials
                WHERE provider = %s
                """,
                (provider.value,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"{provider.value} credential is not configured")
        return cipher.decrypt(
            provider,
            cast(int, row[0]),
            cast(bytes, row[1]),
            cast(bytes, row[2]),
        )

    return ProviderSetupService(inspect=inspect, replace=replace, advance=advance, resolve=resolve)


def resolve_execution_provider_credentials(
    connection: Connection,
    *,
    cipher: CredentialCipher | None,
    jina_fallback: str | None,
    openrouter_fallback: str | None,
    typesafe_fallback: str | None,
) -> ExecutionProviderCredentials:
    rows = connection.execute(
        """
        SELECT provider, generation, nonce, ciphertext
        FROM provider_credentials
        ORDER BY provider
        """
    ).fetchall()
    if rows:
        if cipher is None or len(rows) != len(ProviderKind):
            raise RuntimeError(
                "Database provider credentials are incomplete or cannot be decrypted"
            )
        resolved = {
            ProviderKind(str(row[0])): cipher.decrypt(
                ProviderKind(str(row[0])),
                cast(int, row[1]),
                cast(bytes, row[2]),
                cast(bytes, row[3]),
            )
            for row in rows
        }
        return ExecutionProviderCredentials(
            jina=resolved[ProviderKind.JINA],
            openrouter=resolved[ProviderKind.OPENROUTER],
            typesafe=resolved[ProviderKind.TYPESAFE],
        )
    if jina_fallback is None or openrouter_fallback is None or typesafe_fallback is None:
        raise RuntimeError("Provider credentials are not configured")
    return ExecutionProviderCredentials(
        jina=SecretStr(jina_fallback),
        openrouter=SecretStr(openrouter_fallback),
        typesafe=SecretStr(typesafe_fallback),
    )


def production_provider_validators() -> Mapping[ProviderKind, ProviderValidator]:
    def jina(secret: SecretStr) -> ProviderValidation:
        search, failure = _provider_json_request(
            "https://s.jina.ai/",
            secret,
            body={"q": "site:boards.greenhouse.io software engineer"},
        )
        if failure is not None:
            return failure
        try:
            search_envelope = JinaSearchEnvelope.model_validate(search)
        except ValidationError:
            return ProviderValidation()
        if search_envelope.code != 200:
            return ProviderValidation()
        reader, failure = _provider_json_request(
            "https://r.jina.ai/",
            secret,
            body={"url": "https://example.com"},
        )
        if failure is not None:
            return failure
        try:
            reader_envelope = JinaReaderEnvelope.model_validate(reader)
        except ValidationError:
            return ProviderValidation()
        if reader_envelope.code != 200:
            return ProviderValidation()
        return ProviderValidation(capabilities=REQUIRED_CAPABILITIES[ProviderKind.JINA])

    def openrouter(secret: SecretStr) -> ProviderValidation:
        response, failure = _provider_json_request(
            "https://openrouter.ai/api/v1/chat/completions",
            secret,
            body={
                "model": "google/gemini-2.5-flash-lite",
                "messages": [{"role": "user", "content": 'Return {"ready":true}.'}],
                "max_tokens": 16,
                "usage": {"include": True},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "validate_provider",
                            "description": "Confirm structured generation is available.",
                            "parameters": {
                                "type": "object",
                                "properties": {"ready": {"type": "boolean"}},
                                "required": ["ready"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "validate_provider"},
                },
            },
        )
        if failure is not None:
            return failure
        try:
            completion = OpenRouterCompletion.model_validate(response)
            tool_call = completion.choices[0].message.tool_calls[0]
            if tool_call.function.name != "validate_provider":
                raise ValueError("OpenRouter returned the wrong validation tool")
            _ = _OpenRouterValidationOutput.model_validate_json(tool_call.function.arguments)
        except (IndexError, ValidationError, ValueError):
            return ProviderValidation()
        if completion.usage is None:
            return ProviderValidation()
        return ProviderValidation(capabilities=REQUIRED_CAPABILITIES[ProviderKind.OPENROUTER])

    def typesafe(secret: SecretStr) -> ProviderValidation:
        response, failure = _provider_json_request(
            "https://api.typesafe.ai/v1/systemone",
            secret,
            body={
                "state": "Provider capability validation.",
                "model": "jev-1.13.0",
                "questions": {
                    "validation": {
                        "type": "noul",
                        "instructions": "Is this text a provider capability validation?",
                        "criteria": {"true": "yes", "false": "no"},
                    }
                },
            },
        )
        if failure is not None:
            return failure
        try:
            result = JevSystemOneResponse.model_validate(response)
        except ValidationError:
            return ProviderValidation()
        if "validation" not in result.answers:
            return ProviderValidation()
        return ProviderValidation(capabilities=REQUIRED_CAPABILITIES[ProviderKind.TYPESAFE])

    return {
        ProviderKind.JINA: jina,
        ProviderKind.OPENROUTER: openrouter,
        ProviderKind.TYPESAFE: typesafe,
    }


def _provider_json_request(
    url: str,
    secret: SecretStr,
    *,
    body: dict[str, object],
) -> tuple[object | None, ProviderValidation | None]:
    try:
        response = requests.post(
            url,
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {secret.get_secret_value()}",
                "content-type": "application/json",
            },
            json=body,
            timeout=15,
        )
    except requests.RequestException:
        return None, ProviderValidation()
    if response.status_code in (401, 403):
        return None, ProviderValidation(invalid_credentials=True)
    if not 200 <= response.status_code < 300:
        return None, ProviderValidation()
    try:
        return json.loads(response.text), None
    except json.JSONDecodeError:
        return None, ProviderValidation()
