from __future__ import annotations

import pytest
from pydantic import ValidationError

from job_finder.config import DatabaseSettings, OpenRouterSettings, PostgresContractSettings


def test_database_settings_reads_the_production_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://example/production")

    settings = DatabaseSettings.from_environment()

    assert settings.postgres_dsn == "postgresql://example/production"


def test_database_settings_rejects_a_missing_production_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOB_FINDER_POSTGRES_DSN", raising=False)

    with pytest.raises(ValidationError):
        _ = DatabaseSettings.from_environment()


def test_database_settings_reads_the_test_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_TEST_POSTGRES_DSN", "postgresql://example/test")

    settings = PostgresContractSettings.from_environment()

    assert settings.postgres_dsn == "postgresql://example/test"


def test_openrouter_settings_reads_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    settings = OpenRouterSettings.from_environment()

    assert settings.api_key == "secret"


def test_openrouter_settings_rejects_a_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValidationError):
        _ = OpenRouterSettings.from_environment()
