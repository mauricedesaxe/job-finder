from __future__ import annotations

import pytest
from pydantic import ValidationError

from job_finder.config import (
    CorpusEvaluationSettings,
    DagsterControlSettings,
    DatabaseSettings,
    JevCorpusEvaluationSettings,
    LangfuseSettings,
    OrchestrationSettings,
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
    monkeypatch.setenv("JOB_FINDER_BOOTSTRAP_TOKEN", "b" * 32)
    monkeypatch.setenv("JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY", "credential-key")
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("JOB_FINDER_REVIEW_COOKIE_SECURE", "false")

    settings = ReviewAppSettings.from_environment()

    assert settings.legacy_password is not None
    assert settings.legacy_password.get_secret_value() == "correct horse battery staple"
    assert settings.bootstrap_token is not None
    assert settings.bootstrap_token.get_secret_value() == "b" * 32
    assert settings.credential_encryption_key is not None
    assert settings.credential_encryption_key.get_secret_value() == "credential-key"
    assert settings.session_secret == "s" * 32
    assert not settings.cookie_secure


def test_review_app_settings_require_only_the_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_REVIEW_PASSWORD", "")
    monkeypatch.setenv("JOB_FINDER_BOOTSTRAP_TOKEN", "")
    monkeypatch.setenv("JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY", "")
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "s" * 32)

    settings = ReviewAppSettings.from_environment()

    assert settings.legacy_password is None
    assert settings.bootstrap_token is None
    assert settings.credential_encryption_key is None

    monkeypatch.delenv("JOB_FINDER_REVIEW_SESSION_SECRET")

    with pytest.raises(ValidationError):
        _ = ReviewAppSettings.from_environment()


def test_review_app_settings_reject_the_documented_placeholder_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JOB_FINDER_REVIEW_SESSION_SECRET",
        "replace-with-at-least-32-random-characters",
    )

    with pytest.raises(ValidationError, match="randomly generated"):
        _ = ReviewAppSettings.from_environment()


def test_dagster_control_settings_read_url_and_repo_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_DAGSTER_GRAPHQL_URL", "http://dagster:3000/graphql")
    monkeypatch.delenv("JOB_FINDER_DAGSTER_REPOSITORY_LOCATION", raising=False)
    monkeypatch.delenv("JOB_FINDER_DAGSTER_REPOSITORY_NAME", raising=False)

    settings = DagsterControlSettings.from_environment()

    assert settings is not None
    assert str(settings.graphql_url) == "http://dagster:3000/graphql"
    assert settings.repository_location_name == "job_finder.dagster"
    assert settings.repository_name == "__repository__"
    assert settings.timeout_seconds == 5


def test_dagster_control_settings_skip_when_the_graphql_url_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOB_FINDER_DAGSTER_GRAPHQL_URL", raising=False)

    assert DagsterControlSettings.from_environment() is None

    monkeypatch.setenv("JOB_FINDER_DAGSTER_GRAPHQL_URL", "")

    assert DagsterControlSettings.from_environment() is None


def test_database_settings_reads_the_test_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_TEST_POSTGRES_DSN", "postgresql://example/test")

    settings = PostgresContractSettings.from_environment()

    assert settings.postgres_dsn == "postgresql://example/test"


def test_corpus_settings_require_openrouter_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://example/evaluation")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_REF", "test-ref")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")

    settings = CorpusEvaluationSettings.from_environment()

    assert settings.openrouter_api_key == "openrouter-secret"


def test_railway_commit_supplies_implementation_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://example/production")
    monkeypatch.delenv("JOB_FINDER_IMPLEMENTATION_REF", raising=False)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")

    assert CorpusEvaluationSettings.from_environment().implementation_ref == "a" * 40
    assert OrchestrationSettings.from_environment().implementation_ref == "a" * 40


def test_jev_corpus_settings_only_require_the_typesafe_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-secret")
    monkeypatch.delenv("JOB_FINDER_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("JOB_FINDER_IMPLEMENTATION_REF", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    settings = JevCorpusEvaluationSettings.from_environment()

    assert settings.api_key == "typesafe-secret"
    assert settings.worker_count == 4


def test_orchestration_settings_require_the_typesafe_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://example/production")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-secret")
    monkeypatch.setenv("JINA_API_KEY", "jina-secret")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_REF", "test-ref")

    settings = OrchestrationSettings.from_environment()

    assert settings.typesafe_api_key == "typesafe-secret"


def test_orchestration_provider_keys_can_be_resolved_from_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", "postgresql://example/production")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_REF", "test-ref")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)

    settings = OrchestrationSettings.from_environment()

    assert settings.openrouter_api_key is None
    assert settings.typesafe_api_key is None
    assert settings.jina_api_key is None


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
