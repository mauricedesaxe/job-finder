from __future__ import annotations

import os
from collections.abc import Mapping
from typing import ClassVar

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator


class DatabaseSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        return cls.model_validate({"postgres_dsn": os.environ.get("JOB_FINDER_POSTGRES_DSN")})


class ReviewAppSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    legacy_password: SecretStr | None = None
    bootstrap_token: SecretStr | None = Field(default=None, min_length=32)
    credential_encryption_key: SecretStr | None = None
    session_secret: str = Field(min_length=32)
    cookie_secure: bool = True

    @classmethod
    def from_environment(cls) -> ReviewAppSettings:
        legacy_password = os.environ.get("JOB_FINDER_REVIEW_PASSWORD") or None
        bootstrap_token = os.environ.get("JOB_FINDER_BOOTSTRAP_TOKEN") or None
        credential_encryption_key = os.environ.get("JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY") or None
        return cls.model_validate(
            {
                "legacy_password": legacy_password,
                "bootstrap_token": bootstrap_token,
                "credential_encryption_key": credential_encryption_key,
                "session_secret": os.environ.get("JOB_FINDER_REVIEW_SESSION_SECRET"),
                "cookie_secure": os.environ.get("JOB_FINDER_REVIEW_COOKIE_SECURE", "true")
                == "true",
            }
        )

    @field_validator("session_secret")
    @classmethod
    def reject_example_session_secret(cls, value: str) -> str:
        if value == "replace-with-at-least-32-random-characters":
            raise ValueError("session secret must be randomly generated")
        return value


class DagsterControlSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    graphql_url: AnyHttpUrl
    repository_location_name: str = Field(default="job_finder.dagster", min_length=1)
    repository_name: str = Field(default="__repository__", min_length=1)
    timeout_seconds: int = Field(default=5, gt=0, le=30)

    @classmethod
    def from_environment(cls) -> DagsterControlSettings | None:
        graphql_url = os.environ.get("JOB_FINDER_DAGSTER_GRAPHQL_URL")
        if not graphql_url:
            return None
        return cls.model_validate(
            {
                "graphql_url": graphql_url,
                "repository_location_name": os.environ.get(
                    "JOB_FINDER_DAGSTER_REPOSITORY_LOCATION", "job_finder.dagster"
                ),
                "repository_name": os.environ.get(
                    "JOB_FINDER_DAGSTER_REPOSITORY_NAME", "__repository__"
                ),
                "timeout_seconds": os.environ.get("JOB_FINDER_DAGSTER_TIMEOUT_SECONDS", "5"),
            }
        )


class LangfuseSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", strict=True, validate_default=True
    )

    public_key: str = Field(min_length=1, pattern=r"^pk-lf-")
    secret_key: str = Field(min_length=1, pattern=r"^sk-lf-")
    base_url: AnyHttpUrl
    environment: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")

    @classmethod
    def from_environment(cls) -> LangfuseSettings:
        return cls.model_validate(
            {
                "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY"),
                "secret_key": os.environ.get("LANGFUSE_SECRET_KEY"),
                "base_url": os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
                "environment": os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "production"),
            }
        )

    @classmethod
    def credentials_are_configured(cls, environment: Mapping[str, str] = os.environ) -> bool:
        return bool(environment.get("LANGFUSE_PUBLIC_KEY")) and bool(
            environment.get("LANGFUSE_SECRET_KEY")
        )


class CorpusEvaluationSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)
    openrouter_api_key: str = Field(min_length=1)
    implementation_ref: str = Field(min_length=1)
    worker_count: int = Field(default=12, gt=0, le=32)

    @classmethod
    def from_environment(cls) -> CorpusEvaluationSettings:
        return cls.model_validate(
            {
                "postgres_dsn": os.environ.get("JOB_FINDER_POSTGRES_DSN"),
                "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY"),
                "implementation_ref": os.environ.get("JOB_FINDER_IMPLEMENTATION_REF"),
                "worker_count": os.environ.get("JOB_FINDER_EVAL_WORKER_COUNT", "12"),
            }
        )


class JevCorpusEvaluationSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    api_key: str = Field(min_length=1)
    worker_count: int = Field(default=4, gt=0, le=32)

    @classmethod
    def from_environment(cls) -> JevCorpusEvaluationSettings:
        return cls.model_validate(
            {
                "api_key": os.environ.get("TYPESAFE_API_KEY"),
                "worker_count": os.environ.get("JOB_FINDER_EVAL_WORKER_COUNT", "4"),
            }
        )


class PostgresContractSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)

    @classmethod
    def from_environment(cls) -> PostgresContractSettings:
        return cls.model_validate({"postgres_dsn": os.environ.get("JOB_FINDER_TEST_POSTGRES_DSN")})


class BackfillSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)
    min_body_length: int = Field(default=500, ge=1)

    @classmethod
    def from_environment(cls) -> BackfillSettings:
        return cls.model_validate({"postgres_dsn": os.environ.get("JOB_FINDER_POSTGRES_DSN")})


class OrchestrationSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)
    openrouter_api_key: str | None = None
    typesafe_api_key: str | None = None
    jina_api_key: str | None = None
    credential_encryption_key: SecretStr | None = None
    implementation_ref: str = Field(min_length=1)
    enable_ats_enrichment: bool = True
    search_worker_count: int = Field(default=8, gt=0, le=32)
    work_batch_size: int = Field(default=100, gt=0, le=1000)
    work_lease_seconds: int = Field(default=3600, gt=0)
    work_retry_seconds: int = Field(default=300, ge=0)
    discovery_heartbeat_url: str | None = None
    work_queue_heartbeat_url: str | None = None

    @classmethod
    def from_environment(cls) -> OrchestrationSettings:
        return cls.model_validate(
            {
                "postgres_dsn": os.environ.get("JOB_FINDER_POSTGRES_DSN"),
                "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY") or None,
                "typesafe_api_key": os.environ.get("TYPESAFE_API_KEY") or None,
                "jina_api_key": os.environ.get("JINA_API_KEY") or None,
                "credential_encryption_key": os.environ.get("JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY")
                or None,
                "implementation_ref": os.environ.get("JOB_FINDER_IMPLEMENTATION_REF"),
                "enable_ats_enrichment": os.environ.get("ENABLE_ATS_ENRICHMENT", "true") == "true",
                "search_worker_count": os.environ.get("JOB_FINDER_SEARCH_WORKER_COUNT", "8"),
                "work_batch_size": os.environ.get("JOB_FINDER_WORK_BATCH_SIZE", "100"),
                "work_lease_seconds": os.environ.get("JOB_FINDER_WORK_LEASE_SECONDS", "3600"),
                "work_retry_seconds": os.environ.get("JOB_FINDER_WORK_RETRY_SECONDS", "300"),
                "discovery_heartbeat_url": os.environ.get("JOB_FINDER_DISCOVERY_HEARTBEAT_URL"),
                "work_queue_heartbeat_url": os.environ.get("JOB_FINDER_WORK_QUEUE_HEARTBEAT_URL"),
            }
        )
