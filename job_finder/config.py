from __future__ import annotations

import os
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class DatabaseSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        return cls.model_validate({"postgres_dsn": os.environ.get("JOB_FINDER_POSTGRES_DSN")})


class OpenRouterSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    api_key: str = Field(min_length=1)

    @classmethod
    def from_environment(cls) -> OpenRouterSettings:
        return cls.model_validate({"api_key": os.environ.get("OPENROUTER_API_KEY")})


class PostgresContractSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)

    @classmethod
    def from_environment(cls) -> PostgresContractSettings:
        return cls.model_validate({"postgres_dsn": os.environ.get("JOB_FINDER_TEST_POSTGRES_DSN")})


class OrchestrationSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    postgres_dsn: str = Field(min_length=1)
    openrouter_api_key: str = Field(min_length=1)
    jina_api_key: str = Field(min_length=1)
    implementation_ref: str = Field(min_length=1)
    enable_ats_enrichment: bool = True
    search_worker_count: int = Field(default=8, gt=0, le=32)
    work_batch_size: int = Field(default=100, gt=0, le=1000)
    work_lease_seconds: int = Field(default=3600, gt=0)
    work_retry_seconds: int = Field(default=300, ge=0)

    @classmethod
    def from_environment(cls) -> OrchestrationSettings:
        return cls.model_validate(
            {
                "postgres_dsn": os.environ.get("JOB_FINDER_POSTGRES_DSN"),
                "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY"),
                "jina_api_key": os.environ.get("JINA_API_KEY"),
                "implementation_ref": os.environ.get("JOB_FINDER_IMPLEMENTATION_REF"),
                "enable_ats_enrichment": os.environ.get("ENABLE_ATS_ENRICHMENT", "true") == "true",
                "search_worker_count": os.environ.get("JOB_FINDER_SEARCH_WORKER_COUNT", "8"),
                "work_batch_size": os.environ.get("JOB_FINDER_WORK_BATCH_SIZE", "100"),
                "work_lease_seconds": os.environ.get("JOB_FINDER_WORK_LEASE_SECONDS", "3600"),
                "work_retry_seconds": os.environ.get("JOB_FINDER_WORK_RETRY_SECONDS", "300"),
            }
        )
