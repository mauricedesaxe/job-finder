from __future__ import annotations

import pytest
from pydantic import ValidationError

from job_finder.config import (
    DatabaseSettings,
    LangfuseSettings,
    OpenRouterSettings,
    PostgresContractSettings,
    ReviewAppSettings,
)


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


def test_review_app_settings_require_production_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_REVIEW_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("JOB_FINDER_REVIEW_COOKIE_SECURE", "false")

    settings = ReviewAppSettings.from_environment()

    assert settings.app_password == "correct horse battery staple"
    assert settings.session_secret == "s" * 32
    assert not settings.cookie_secure


def test_review_app_settings_reject_missing_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_FINDER_REVIEW_PASSWORD", raising=False)
    monkeypatch.delenv("JOB_FINDER_REVIEW_SESSION_SECRET", raising=False)

    with pytest.raises(ValidationError):
        _ = ReviewAppSettings.from_environment()


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


def test_langfuse_settings_require_valid_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "production")

    settings = LangfuseSettings.from_environment()

    assert settings.public_key == "pk-lf-public"
    assert settings.secret_key == "sk-lf-secret"
    assert str(settings.base_url) == "https://us.cloud.langfuse.com/"
    assert settings.environment == "production"


def test_langfuse_settings_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "not-a-url")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "Production EU")

    with pytest.raises(ValidationError):
        _ = LangfuseSettings.from_environment()


def test_langfuse_schedule_requires_both_credentials() -> None:
    assert not LangfuseSettings.credentials_are_configured({})
    assert not LangfuseSettings.credentials_are_configured({"LANGFUSE_PUBLIC_KEY": "pk-lf-public"})
    assert LangfuseSettings.credentials_are_configured(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-lf-public",
            "LANGFUSE_SECRET_KEY": "sk-lf-secret",
        }
    )
