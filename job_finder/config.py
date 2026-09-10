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
