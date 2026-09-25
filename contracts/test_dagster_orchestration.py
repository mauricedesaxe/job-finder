from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ConfigurationActivated,
    activate_search_configuration,
    load_published_active_search_configuration,
)
from job_finder.database import apply_migrations
from job_finder.dagster import defs
from job_finder.ats.models import AtsAvailable
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import ScrapeSucceeded, SearchSucceeded
from job_finder.evaluation.openrouter import HttpResponse, RetryPolicy
from job_finder.evaluation.jev import JEV_MODEL, JevHttpResponse, JevRetryPolicy
from job_finder.evaluation.prompt_releases import build_prompt_release, store_prompt_release
from job_finder.evaluation.release_targets import get_active_release_target
from job_finder.execution_budget import postgres_budget_setup_service
from job_finder.pipeline.orchestration import (
    PipelineBoundaries,
    ProcessingSummary,
    discover_jobs,
    process_claimed_jobs,
)
from job_finder.pipeline.work_items import (
    JobWorkClaim,
    claim_next_job,
    complete_job_claim,
    fail_job_claim,
)
from job_finder.pipeline.runs import fail_orchestration_run, prepare_orchestration_run
from job_finder.pipeline.discoveries import register_discoveries
from job_finder.review.operations import (
    JobReevaluationAccepted,
    JobReevaluationCommand,
    JobReevaluationUnsupported,
    request_job_reevaluation,
)
from job_finder.review.owner_access import postgres_owner_access_service
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    SupportedSearchSource,
    build_search_configuration_revision,
    store_search_configuration_revision,
)

_LONG_MARKDOWN = (
    "# Senior Product Engineer\n" + "Build the product with a strong remote team. " * 20
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_dagster_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_run_retry_reuses_frozen_configuration_pair_and_exchange_rates(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        first = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:run-1",
            implementation_ref="commit-1",
            started_at=now,
            load_active_configuration=load_published_active_search_configuration,
            fetch_rates=lambda: rates,
        )
        fail_orchestration_run(
            connection,
            first.id,
            completed_at=now + timedelta(minutes=1),
            error_code="crash",
            reason="worker stopped",
        )
        second = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:run-1",
            implementation_ref="commit-1",
            started_at=now + timedelta(hours=1),
            load_active_configuration=lambda _connection: pytest.fail(
                "active configuration was reloaded"
            ),
            fetch_rates=lambda: pytest.fail("rates were refetched"),
        )

    assert second.id == first.id
    assert second.status == "running"
    assert second.configuration_revision_id == first.configuration_revision_id
    assert second.prompt_release_id == first.prompt_release_id
    assert second.target == first.target
    assert second.exchange_rates == rates


def test_configuration_activation_only_changes_new_orchestration_run_configuration(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    searches: list[tuple[str, str]] = []

    def search(keyword: str, domain: str) -> SearchSucceeded:
        searches.append((keyword, domain))
        return SearchSucceeded(urls=())

    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_published_active_search_configuration(connection)
        first = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:configuration-pair-1",
            implementation_ref="commit-1",
            started_at=now,
            load_active_configuration=load_published_active_search_configuration,
            fetch_rates=lambda: rates,
        )
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "search_keywords": ("configured search",),
                "enabled_sources": (SupportedSearchSource.LEVER,),
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"instructions": "Use the newly activated criterion."}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                ),
            }
        )
        revision = build_search_configuration_revision(
            changed_configuration,
            created_at=now + timedelta(minutes=1),
            created_by="owner",
        )
        _ = store_search_configuration_revision(connection, revision)
        release = store_prompt_release(
            connection,
            build_prompt_release(changed_configuration),
            created_at=now + timedelta(minutes=1),
            created_by="owner",
        )
        connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, 'owner')
            """,
            (revision.id, release.id, now + timedelta(minutes=1)),
        )
        activated = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=revision.id,
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now + timedelta(minutes=2),
            ),
        )
        assert isinstance(activated, ConfigurationActivated)

        resumed = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:configuration-pair-1",
            implementation_ref="commit-1",
            started_at=now + timedelta(minutes=3),
            load_active_configuration=lambda _connection: pytest.fail(
                "active configuration was reloaded"
            ),
            fetch_rates=lambda: pytest.fail("rates were refetched"),
        )
        second = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:configuration-pair-2",
            implementation_ref="commit-1",
            started_at=now + timedelta(minutes=3),
            load_active_configuration=load_published_active_search_configuration,
            fetch_rates=lambda: rates,
        )
        discovery = discover_jobs(
            connection,
            second,
            PipelineBoundaries(
                search=search,
                scrape=lambda _url: pytest.fail("scrape was called"),
                fetch_ats=lambda _url, _title: pytest.fail("ATS was called"),
            ),
            discovered_at=now + timedelta(minutes=3),
            max_workers=1,
        )

    assert first.configuration_revision_id == initial.publication.revision_id
    assert first.prompt_release_id == initial.publication.prompt_release_id
    assert resumed.configuration_revision_id == first.configuration_revision_id
    assert resumed.prompt_release_id == first.prompt_release_id
    assert second.configuration_revision_id == revision.id
    assert second.prompt_release_id == first.prompt_release_id
    assert second.target == first.target
    assert discovery.query_count == 1
    assert searches == [("configured search", "jobs.lever.co")]


def test_exact_url_registration_precedes_exclusive_leased_work(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.ashbyhq.com/acme/one"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:run-2",
            implementation_ref="commit-1",
            started_at=now,
            load_active_configuration=load_published_active_search_configuration,
            fetch_rates=lambda: ExchangeRateSnapshot(
                rates={"EUR": Decimal("1.11")},
                source="frankfurter",
                observed_at=now,
            ),
        )
        first_registration = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        duplicate_registration = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )

    first_owner = uuid4()
    second_owner = uuid4()
    with _connection(authority_schema) as first_connection:
        first_claim = claim_next_job(
            first_connection,
            owner_token=first_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
    with _connection(authority_schema) as second_connection:
        concurrent_claim = claim_next_job(
            second_connection,
            owner_token=second_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        reclaimed = claim_next_job(
            second_connection,
            owner_token=second_owner,
            claimed_at=now + timedelta(minutes=6),
            lease_for=timedelta(minutes=5),
        )

    assert first_registration.discovered_count == 1
    assert first_registration.new_work_count == 1
    assert duplicate_registration.discovered_count == 0
    assert duplicate_registration.new_work_count == 0
    assert first_claim is not None
    assert first_claim.raw_url == raw_url
    assert concurrent_claim is None
    assert reclaimed is not None
    assert reclaimed.job_id == first_claim.job_id
    assert reclaimed.attempt_count == 2


def test_failed_work_becomes_claimable_after_retry_time(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:run-3",
            implementation_ref="commit-1",
            started_at=now,
            load_active_configuration=load_published_active_search_configuration,
            fetch_rates=lambda: ExchangeRateSnapshot(
                rates={"EUR": Decimal("1.11")},
                source="frankfurter",
                observed_at=now,
            ),
        )
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=("https://jobs.ashbyhq.com/acme/two",),
            discovered_at=now,
        )
        claim = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claim is not None
        assert (
            fail_job_claim(
                connection,
                claim,
                failed_at=now,
                retry_after=timedelta(minutes=2),
                error_code="reader_unavailable",
                reason="timeout",
            )
            == "retry"
        )
        too_early = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=now + timedelta(minutes=1),
            lease_for=timedelta(minutes=5),
        )
        retried = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=now + timedelta(minutes=2),
            lease_for=timedelta(minutes=5),
        )

    assert too_early is None
    assert retried is not None
    assert retried.attempt_count == 2


def test_dagster_job_executes_the_domain_cycle(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    settings = PostgresContractSettings.from_environment()
    schema_dsn = make_conninfo(settings.postgres_dsn, options=f"-c search_path={authority_schema}")
    boundaries = PipelineBoundaries(
        search=lambda _keyword, _domain: SearchSucceeded(urls=()),
        scrape=lambda _url: pytest.fail("empty discovery reached the reader"),
        fetch_ats=lambda _url, _title: pytest.fail("empty discovery reached ATS"),
    )
    monkeypatch.setenv("JOB_FINDER_POSTGRES_DSN", schema_dsn)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unused")
    monkeypatch.setenv("JINA_API_KEY", "unused")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_REF", "commit-1")

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
    owner = postgres_owner_access_service(lambda: _connection(authority_schema))
    _ = owner.bootstrap("secure contract owner password")
    with _connection(authority_schema) as connection:
        for stage in ("preferences", "budget"):
            _ = connection.execute(
                """
                UPDATE owner_onboarding
                SET stage = %s, updated_at = CURRENT_TIMESTAMP
                WHERE singleton_id = 1
                """,
                (stage,),
            )
    _ = postgres_budget_setup_service(lambda: _connection(authority_schema)).save(
        0,
        Decimal("20"),
        Decimal("2"),
        25,
        "contract-owner",
        now,
    )
    with _connection(authority_schema) as connection:
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'complete', updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """
        )

    def fixed_boundaries(*, jina_api_key: str) -> PipelineBoundaries:
        assert jina_api_key == "unused"
        return boundaries

    def fixed_rates(_observed_at: datetime) -> ExchangeRateSnapshot:
        return ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
        )

    monkeypatch.setattr("job_finder.dagster.production_boundaries", fixed_boundaries)
    monkeypatch.setattr("job_finder.dagster._fetch_rates", fixed_rates)

    result = defs.resolve_job_def("job_finder").execute_in_process()

    assert result.success
    with _connection(authority_schema) as connection:
        stored = connection.execute(
            """
            SELECT status, implementation_ref, configuration_revision_id,
                   prompt_release_id, relevance_release_id
            FROM pipeline_runs
            WHERE kind = 'orchestration'
            """
        ).fetchone()
        publication = load_published_active_search_configuration(connection).publication
        active_target = get_active_release_target(connection)
    assert stored == (
        "completed",
        "commit-1",
        publication.revision_id,
        publication.prompt_release_id,
        active_target.target.relevance_release_id,
    )


def test_database_rejects_incomplete_work_and_attempt_failure_states(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/invalid-failure-state"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-invalid-failure-state", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        claim = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claim is not None

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE job_work_items
                SET state = 'terminal_error', owner_token = NULL, lease_expires_at = NULL
                WHERE job_id = %s
                """,
                (claim.job_id,),
            )

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, job_id, operation_key, attempt_number,
                  input_digest, status, started_at, completed_at, error
                ) VALUES (%s, %s, %s, 'evaluation:test', 0, %s, 'failed', %s, %s,
                  '{"code":"timeout","reason":"timed out"}'::jsonb)
                """,
                (uuid4(), run.id, claim.job_id, "f" * 64, now, now),
            )


def test_known_exact_url_never_reaches_the_reader(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.ashbyhq.com/acme/known"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-known", now)
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, %s, %s, %s)
            """,
            (uuid4(), raw_url, now - timedelta(days=1), now - timedelta(days=1)),
        )
        registration = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: pytest.fail("known URL reached the Jina reader"),
                fetch_ats=lambda _url, _title: pytest.fail("known URL reached the ATS API"),
            ),
            openrouter_api_key="unused",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=True,
        )

    assert registration.discovered_count == 1
    assert registration.new_work_count == 0
    assert summary.claimed_count == 0


def test_ats_rejection_and_claim_completion_commit_together(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.ashbyhq.com/acme/onsite"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-ats", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: AtsAvailable(
                    source="ashby",
                    location="London",
                    locations=("London",),
                    workplace_type="OnSite",
                    country="GB",
                ),
                model_sender=_unexpected_model_call,
            ),
            openrouter_api_key="unused",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=True,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, d.decision_stage, w.state, w.terminal_decision_id = d.id
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()
        review_item_count = connection.execute("SELECT count(*) FROM review_items").fetchone()

    assert summary.terminal_count == 1
    assert stored == ("rejected", "ats_structural", "completed", True)
    assert review_item_count == (0,)


def test_an_active_company_policy_suppresses_the_job_before_ats_and_models(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/senior-product-engineer"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-policy", now)
        connection.execute(
            """
            INSERT INTO company_policies (
              normalized_company, company, policy, effective_at, expires_at
            ) VALUES ('acme', 'Acme', 'recent_application', %s, %s)
            """,
            (now - timedelta(days=1), now + timedelta(days=180)),
        )
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS must not run for a suppressed job"),
                model_sender=_unexpected_model_call,
            ),
            openrouter_api_key="unused",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=True,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, d.decision_stage, w.state, w.terminal_decision_id = d.id
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()
        review_item_count = connection.execute("SELECT count(*) FROM review_items").fetchone()
        model_call_count = connection.execute("SELECT count(*) FROM model_call_attempts").fetchone()

    assert summary.terminal_count == 1
    assert stored == ("company_applied", "company_policy", "completed", True)
    assert review_item_count == (0,)
    assert model_call_count == (0,)


def test_structural_rejection_persists_a_terminal_decision(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-structural", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
            ),
            openrouter_api_key="unused",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, d.decision_stage, w.state
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()

    assert summary.terminal_count == 1
    assert stored == ("rejected", "structural", "completed")


def test_ats_description_replaces_a_thin_scrape(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.ashbyhq.com/acme/onsite-thin"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-ats-description", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown="Overview"),
                fetch_ats=lambda _url, _title: AtsAvailable(
                    source="ashby",
                    location="Berlin",
                    locations=("Berlin",),
                    workplace_type="OnSite",
                    country=None,
                    description="YOUR MISSION\n\n" + "Own features end to end. " * 30,
                ),
                model_sender=_unexpected_model_call,
            ),
            openrouter_api_key="unused",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=True,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, s.description LIKE %s
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE s.raw_url = %s
            """,
            ("%Own features end to end.%", raw_url),
        ).fetchone()

    assert summary.terminal_count == 1
    assert stored == ("rejected", True)


def test_a_thin_scrape_retries_then_dead_letters(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://example.com/careers/thin-role"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-thin-scrape", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="example.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        boundaries = PipelineBoundaries(
            search=lambda _keyword, _domain: SearchSucceeded(urls=()),
            scrape=lambda _url: ScrapeSucceeded(markdown="# Senior Product Engineer\nOverview"),
            fetch_ats=lambda _url, _title: pytest.fail("ATS must not run for unknown sources"),
            model_sender=_unexpected_model_call,
        )

        def attempt() -> ProcessingSummary:
            return process_claimed_jobs(
                connection,
                run,
                boundaries,
                openrouter_api_key="unused",
                owner_token=uuid4(),
                observed_at=now,
                max_items=1,
                lease_for=timedelta(minutes=5),
                retry_after=timedelta(0),
                enable_ats_enrichment=False,
                now=lambda: now,
            )

        first = attempt()
        second = attempt()
        third = attempt()
        fourth = attempt()
        work = connection.execute(
            """
            SELECT state, attempt_count, last_error->>'code'
            FROM job_work_items
            """
        ).fetchone()
        decision_count = connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone()

    assert first.retry_scheduled_count == 1
    assert second.retry_scheduled_count == 1
    assert third.terminal_error_count == 1
    assert fourth.claimed_count == 0
    assert work == ("terminal_error", 3, "thin_scrape")
    assert decision_count == (0,)


def test_an_unexpected_work_failure_retries_then_dead_letters(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://example.com/careers/crashing-role"

    def crash(_url: str) -> ScrapeSucceeded:
        raise RuntimeError("reader crashed")

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-crashing-work", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="example.com",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        boundaries = PipelineBoundaries(
            search=lambda _keyword, _domain: SearchSucceeded(urls=()),
            scrape=crash,
            fetch_ats=lambda _url, _title: pytest.fail("crashing scrape reached ATS"),
        )

        def attempt() -> ProcessingSummary:
            return process_claimed_jobs(
                connection,
                run,
                boundaries,
                openrouter_api_key="unused",
                owner_token=uuid4(),
                observed_at=now,
                max_items=1,
                lease_for=timedelta(minutes=5),
                retry_after=timedelta(0),
                enable_ats_enrichment=False,
                now=lambda: now,
            )

        with pytest.raises(RuntimeError, match="reader crashed"):
            _ = attempt()
        with pytest.raises(RuntimeError, match="reader crashed"):
            _ = attempt()
        with pytest.raises(RuntimeError, match="reader crashed"):
            _ = attempt()
        fourth = attempt()
        work = connection.execute(
            """
            SELECT state, attempt_count, completed_at, retry_at,
                   last_error->>'code', last_error->>'reason',
                   last_error->>'retryability'
            FROM job_work_items
            """
        ).fetchone()

    assert fourth.claimed_count == 0
    assert work == (
        "terminal_error",
        3,
        now,
        None,
        "RuntimeError",
        "reader crashed",
        "retryable",
    )


def test_llm_rejection_persists_every_model_attempt_and_terminal_state(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/llm-reject"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-llm-reject", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
                jev_sender=_rejecting_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, d.decision_stage, w.state
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()
        model_attempt_count = connection.execute(
            "SELECT count(*) FROM model_call_attempts"
        ).fetchone()

    assert summary.terminal_count == 1
    assert stored == ("rejected", "evaluation", "completed")
    assert model_attempt_count == (4,)


def test_retryable_model_attempt_resumes_and_completes_after_acceptance(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    retry_at = now + timedelta(minutes=2)
    raw_url = "https://jobs.lever.co/acme/retryable-model-error"
    one_attempt = RetryPolicy(max_attempts=1, base_delay_seconds=0)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-retryable-model-error", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        first = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
                model_retry_policy=one_attempt,
                jev_sender=_retryable_jev_call,
                jev_retry_policy=JevRetryPolicy(max_attempts=1, base_delay_seconds=0),
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
            now=lambda: now,
        )
        failed_work = connection.execute(
            """
            SELECT state, retry_at, completed_at, last_error->>'retryability'
            FROM job_work_items
            """
        ).fetchone()
        failed_attempts = connection.execute(
            """
            SELECT status, completed_at IS NOT NULL, error->>'retryability'
            FROM processing_attempts
            ORDER BY operation_key
            """
        ).fetchall()

        assert first.retry_scheduled_count == 1
        assert failed_work == ("failed", retry_at, None, "retryable")
        assert failed_attempts == [("failed", True, "retryable")] * 4

        model_outputs: Iterator[tuple[str, Mapping[str, object]]] = iter(
            (
                (
                    "enrich_job",
                    {
                        "title": "Senior Product Engineer",
                        "company": "Acme",
                        "description": "Build the product.",
                        "location": "Remote",
                    },
                ),
            )
        )

        def accepted_sender(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout: float,
        ) -> HttpResponse:
            tool_name, output = next(model_outputs)
            return _model_response(tool_name, output)

        second = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=accepted_sender,
                model_retry_policy=one_attempt,
                jev_sender=_qualifying_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=retry_at,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
            now=lambda: retry_at,
        )
        resumed = connection.execute(
            """
            SELECT p.status, p.error, array_agg(m.status ORDER BY m.attempt_number)
            FROM processing_attempts p
            JOIN model_call_attempts m ON m.processing_attempt_id = p.id
            WHERE p.operation_key = 'evaluation:job-finder-filter-location-eligibility'
            GROUP BY p.status, p.error
            """
        ).fetchone()
        completed_work = connection.execute(
            "SELECT state, terminal_decision_id IS NOT NULL FROM job_work_items"
        ).fetchone()

    assert second.terminal_count == 1
    assert resumed == ("completed", None, ["retryable_error", "accepted"])
    assert completed_work == ("completed", True)


def test_crash_after_terminal_model_attempt_converges_from_the_cached_error(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    retry_at = now + timedelta(minutes=2)
    raw_url = "https://jobs.lever.co/acme/terminal-crash"

    def crash_before_dead_letter(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("worker stopped before dead-letter write")

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-terminal-crash", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(
                "job_finder.pipeline.orchestration.terminally_fail_job_claim",
                crash_before_dead_letter,
            )
            with pytest.raises(RuntimeError, match="dead-letter write"):
                _ = process_claimed_jobs(
                    connection,
                    run,
                    PipelineBoundaries(
                        search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                        scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                        fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                        model_sender=_unexpected_model_call,
                        jev_sender=_terminal_jev_call,
                    ),
                    openrouter_api_key="test-key",
                    typesafe_api_key="test-key",
                    owner_token=uuid4(),
                    observed_at=now,
                    max_items=1,
                    lease_for=timedelta(minutes=5),
                    retry_after=timedelta(minutes=2),
                    enable_ats_enrichment=False,
                    now=lambda: now,
                )
        after_crash = connection.execute("SELECT state, retry_at FROM job_work_items").fetchone()
        attempt_count_after_crash = connection.execute(
            "SELECT count(*) FROM model_call_attempts"
        ).fetchone()

        recovered = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
                jev_sender=_unexpected_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=retry_at,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
            now=lambda: retry_at,
        )
        final_work = connection.execute(
            "SELECT state, completed_at, retry_at FROM job_work_items"
        ).fetchone()
        final_model_attempt_count = connection.execute(
            "SELECT count(*) FROM model_call_attempts"
        ).fetchone()

    assert after_crash == ("failed", retry_at)
    assert attempt_count_after_crash == (4,)
    assert recovered.terminal_error_count == 1
    assert final_work == ("terminal_error", retry_at, None)
    assert final_model_attempt_count == attempt_count_after_crash


def test_terminal_jev_error_dead_letters_work_without_a_decision(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/terminal-model-error"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-terminal-model-error", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        first = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
                jev_sender=_terminal_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
            now=lambda: now,
        )
        work = connection.execute(
            """
            SELECT state, completed_at IS NOT NULL, last_error->>'retryability',
                   terminal_decision_id
            FROM job_work_items
            """
        ).fetchone()
        decision_count = connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone()
        processing_attempts = connection.execute(
            """
            SELECT status, completed_at IS NOT NULL, error->>'retryability'
            FROM processing_attempts
            ORDER BY operation_key
            """
        ).fetchall()

        assert work == ("terminal_error", True, "terminal", None)
        assert first.terminal_error_count == 1
        assert decision_count == (0,)
        assert processing_attempts == [("failed", True, "terminal")] * 4

        second = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: pytest.fail("dead-lettered work reached the reader"),
                fetch_ats=lambda _url, _title: pytest.fail("dead-lettered work reached ATS"),
                model_sender=_unexpected_model_call,
            ),
            openrouter_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now + timedelta(hours=1),
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
            now=lambda: now + timedelta(hours=1),
        )

    assert second.claimed_count == 0


def test_qualified_job_runs_evaluation_enrichment_and_deduplication(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/qualified"
    model_outputs: Iterator[tuple[str, Mapping[str, object]]] = iter(
        (
            (
                "enrich_job",
                {
                    "title": "Senior Product Engineer",
                    "company": "Acme",
                    "description": "Build the product.",
                    "location": "Remote",
                },
            ),
        )
    )

    def model_sender(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        tool_name, output = next(model_outputs)
        return _model_response(tool_name, output)

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-qualified", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=model_sender,
                jev_sender=_qualifying_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, d.decision_stage, d.matched_profile, w.state,
                   s.title, s.company, s.location
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()
        counts = connection.execute(
            """
            SELECT (SELECT count(*) FROM model_call_attempts),
                   (SELECT count(*) FROM processing_attempts)
            """
        ).fetchone()
        review_item = connection.execute(
            """
            SELECT i.lane, i.review_day, i.evaluation_id = d.id
            FROM review_items i
            JOIN evaluation_decisions d ON d.id = i.evaluation_id
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()
        jev_provenance = connection.execute(
            """
            SELECT raw_response, input_tokens, output_tokens, cost_usd, response_model
            FROM model_call_attempts
            WHERE status = 'accepted' AND provider = 'typesafe'
            """
        ).fetchall()

    assert summary.terminal_count == 1
    assert stored == (
        "qualified",
        "qualified",
        "early-stage-product-engineer",
        "completed",
        "Senior Product Engineer",
        "Acme",
        "Remote",
    )
    assert counts == (7, 8)
    assert review_item == ("qualified", now.date(), True)
    assert len(jev_provenance) == 6
    for raw_response, input_tokens, output_tokens, cost_usd, response_model in jev_provenance:
        assert raw_response is not None
        assert cast(dict[str, object], raw_response)["answers"]
        assert cast(int, input_tokens) > 0
        assert cast(int, output_tokens) > 0
        assert cost_usd is not None
        assert response_model == JEV_MODEL


def test_rolls_back_qualified_terminal_state_when_claim_completion_fails(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/rollback"
    model_outputs: Iterator[tuple[str, Mapping[str, object]]] = iter(
        (
            (
                "enrich_job",
                {
                    "title": "Senior Product Engineer",
                    "company": "Acme",
                    "description": "Build the product.",
                    "location": "Remote",
                },
            ),
        )
    )

    def model_sender(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        tool_name, output = next(model_outputs)
        return _model_response(tool_name, output)

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-rollback", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )

        def fail_after_claim_completion(
            completion_connection: psycopg.Connection[tuple[object, ...]],
            claim: JobWorkClaim,
            *,
            decision_id: str,
            completed_at: datetime,
        ) -> bool:
            assert completion_connection is connection
            assert complete_job_claim(
                completion_connection,
                claim,
                decision_id=decision_id,
                completed_at=completed_at,
            )
            terminal_state = completion_connection.execute(
                """
                SELECT (SELECT count(*) FROM job_snapshots WHERE job_id = %s),
                       (SELECT count(*)
                        FROM evaluation_decisions d
                        JOIN job_snapshots s ON s.id = d.snapshot_id
                        WHERE s.job_id = %s),
                       (SELECT count(*) FROM pipeline_receipts
                        WHERE operation_key = 'process_qualified_job' AND job_id = %s),
                       (SELECT count(*) FROM review_items
                        WHERE evaluation_id = %s AND lane = 'qualified'),
                       state, terminal_decision_id
                FROM job_work_items
                WHERE job_id = %s
                """,
                (claim.job_id, claim.job_id, claim.job_id, decision_id, claim.job_id),
            ).fetchone()
            assert terminal_state == (1, 1, 1, 1, "completed", decision_id)
            raise RuntimeError("claim completion failed")

        with monkeypatch.context() as completion_patch:
            completion_patch.setattr(
                "job_finder.pipeline.orchestration.complete_job_claim",
                fail_after_claim_completion,
            )
            with pytest.raises(RuntimeError, match="claim completion failed"):
                _ = process_claimed_jobs(
                    connection,
                    run,
                    PipelineBoundaries(
                        search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                        scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                        fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                        model_sender=model_sender,
                        jev_sender=_qualifying_jev_call,
                    ),
                    openrouter_api_key="test-key",
                    typesafe_api_key="test-key",
                    owner_token=uuid4(),
                    observed_at=now,
                    max_items=1,
                    lease_for=timedelta(minutes=5),
                    retry_after=timedelta(minutes=2),
                    enable_ats_enrichment=False,
                    now=lambda: now,
                )

        rolled_back = connection.execute(
            """
            SELECT (SELECT count(*) FROM job_snapshots WHERE raw_url = %s),
                   (SELECT count(*)
                    FROM evaluation_decisions d
                    JOIN job_snapshots s ON s.id = d.snapshot_id
                    WHERE s.raw_url = %s),
                   (SELECT count(*)
                    FROM pipeline_receipts r
                    JOIN jobs j ON j.id = r.job_id
                    WHERE r.operation_key = 'process_qualified_job' AND j.raw_url = %s),
                   (SELECT count(*)
                    FROM review_items i
                    JOIN evaluation_decisions d ON d.id = i.evaluation_id
                    JOIN job_snapshots s ON s.id = d.snapshot_id
                    WHERE s.raw_url = %s AND i.lane = 'qualified'),
                   w.state, w.terminal_decision_id, w.completed_at, w.owner_token
            FROM job_work_items w
            JOIN jobs j ON j.id = w.job_id
            WHERE j.raw_url = %s
            """,
            (raw_url, raw_url, raw_url, raw_url, raw_url),
        ).fetchone()

    assert rolled_back == (0, 0, 0, 0, "failed", None, None, None)


def test_reevaluation_processes_the_pinned_snapshot_without_rewriting_history(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/reevaluate"
    long_description = "Build reliable AI products with a remote team. " * 20

    def sender(outputs: Iterator[tuple[str, Mapping[str, object]]]):
        def send(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout: float,
        ) -> HttpResponse:
            tool_name, output = next(outputs)
            return _model_response(tool_name, output)

        return send

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        source_run = _prepare_run(connection, "dagster:reevaluation-source", now)
        _ = register_discoveries(
            connection,
            run_id=source_run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        first = process_claimed_jobs(
            connection,
            source_run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: AtsAvailable(
                    source="ashby",
                    description=long_description,
                    location="Remote",
                    locations=("Remote",),
                    workplace_type="Remote",
                    country="US",
                ),
                model_sender=sender(
                    iter(
                        (
                            (
                                "enrich_job",
                                {
                                    "title": "Senior Product Engineer",
                                    "company": "Acme",
                                    "description": long_description,
                                    "location": "Remote",
                                },
                            ),
                        )
                    )
                ),
                jev_sender=_qualifying_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=True,
            now=lambda: now,
        )
        source = connection.execute(
            """
            SELECT d.id, d.snapshot_id, i.id, s.job_id
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN review_items i ON i.evaluation_id = d.id
            WHERE s.raw_url = %s
            """,
            (raw_url,),
        ).fetchone()
        assert source is not None
        source_decision_id = str(source[0])
        source_snapshot_id = str(source[1])
        source_review_item_id = source[2]
        job_id = source[3]
        connection.execute(
            """
            INSERT INTO review_events (
              id, review_item_id, decision, target_profile, primary_reason,
              block_company, actor, created_at
            ) VALUES (%s, %s, 'unsure', 'early-stage-product-engineer', 'other',
              false, 'owner', %s)
            """,
            (uuid4(), source_review_item_id, now),
        )
        request = request_job_reevaluation(
            connection,
            JobReevaluationCommand(
                idempotency_key="reevaluate-qualified-job",
                expected_decision_id=source_decision_id,
                expected_snapshot_id=source_snapshot_id,
                actor="owner",
                requested_at=now + timedelta(minutes=1),
            ),
        )
        assert isinstance(request, JobReevaluationAccepted)

        second = process_claimed_jobs(
            connection,
            source_run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: pytest.fail("reevaluation reached the Jina reader"),
                fetch_ats=lambda _url, _title: pytest.fail("reevaluation reached ATS"),
                model_sender=sender(
                    iter(
                        (
                            (
                                "enrich_job",
                                {
                                    "title": "Senior Product Engineer",
                                    "company": "Acme",
                                    "description": long_description,
                                    "location": "Remote",
                                },
                            ),
                            (
                                "check_duplicate",
                                {"isDuplicate": False, "matchedTitle": None},
                            ),
                        )
                    )
                ),
                jev_sender=_qualifying_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now + timedelta(minutes=2),
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=True,
            now=lambda: now + timedelta(minutes=2),
        )
        latest_decision = connection.execute(
            "SELECT terminal_decision_id FROM job_work_items WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert latest_decision is not None
        connection.execute(
            """
            INSERT INTO snapshot_corrections (snapshot_id, reason, created_at)
            VALUES (%s, 'Manual correction', %s)
            """,
            (source_snapshot_id, now + timedelta(minutes=3)),
        )
        unsupported = request_job_reevaluation(
            connection,
            JobReevaluationCommand(
                idempotency_key="reevaluate-corrected-snapshot",
                expected_decision_id=str(latest_decision[0]),
                expected_snapshot_id=source_snapshot_id,
                actor="owner",
                requested_at=now + timedelta(minutes=3),
            ),
        )
        assert isinstance(unsupported, JobReevaluationUnsupported)
        assert unsupported.receipt.conflict_code == "corrected_snapshot"
        decisions = connection.execute(
            """
            SELECT id, snapshot_id, prompt_release_id, relevance_release_id,
                   source_snapshot_id, predecessor_decision_id,
                   reevaluation_request_key, pipeline_run_id, outcome
            FROM evaluation_decisions
            WHERE snapshot_id = %s
            ORDER BY created_at, id
            """,
            (source_snapshot_id,),
        ).fetchall()
        work = connection.execute(
            """
            SELECT state, terminal_decision_id, active_reevaluation_key
            FROM job_work_items WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
        review_counts = connection.execute(
            """
            SELECT (SELECT count(*) FROM review_items),
                   (SELECT count(*) FROM review_events)
            """
        ).fetchone()
        reevaluation_run = connection.execute(
            """
            SELECT status, prompt_release_id, relevance_release_id
            FROM pipeline_runs WHERE id = %s
            """,
            (request.receipt.reevaluation_pipeline_run_id,),
        ).fetchone()
        reevaluation_model_calls = connection.execute(
            """
            SELECT count(*) FROM model_call_attempts
            WHERE pipeline_run_id = %s
            """,
            (request.receipt.reevaluation_pipeline_run_id,),
        ).fetchone()

    assert first.terminal_count == 1
    assert second.terminal_count == 1
    assert len(decisions) == 2
    original = next(row for row in decisions if str(row[0]) == source_decision_id)
    reevaluated = next(row for row in decisions if str(row[0]) != source_decision_id)
    assert original[4:7] == (None, None, None)
    assert reevaluated[1] == source_snapshot_id
    assert reevaluated[2] == request.receipt.prompt_release_id
    assert reevaluated[3] == request.receipt.relevance_release_id
    assert reevaluated[4:7] == (
        source_snapshot_id,
        source_decision_id,
        request.receipt.idempotency_key,
    )
    assert reevaluated[7] == request.receipt.reevaluation_pipeline_run_id
    assert reevaluated[8] == "qualified"
    assert work == (
        "completed",
        reevaluated[0],
        request.receipt.idempotency_key,
    )
    assert review_counts == (2, 1)
    assert reevaluation_run == (
        "completed",
        request.receipt.prompt_release_id,
        request.receipt.relevance_release_id,
    )
    assert reevaluation_model_calls is not None
    assert int(str(reevaluation_model_calls[0])) > 0


def test_profile_stage_rejection_runs_every_criterion_before_rejecting(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    raw_url = "https://jobs.lever.co/acme/profile-reject"
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        run = _prepare_run(connection, "dagster:run-profile-reject", now)
        _ = register_discoveries(
            connection,
            run_id=run.id,
            keyword="senior product engineer",
            domain="jobs.lever.co",
            raw_urls=(raw_url,),
            discovered_at=now,
        )
        summary = process_claimed_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=lambda _keyword, _domain: SearchSucceeded(urls=()),
                scrape=lambda _url: ScrapeSucceeded(markdown=_LONG_MARKDOWN),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
                jev_sender=_signal_free_jev_call,
            ),
            openrouter_api_key="test-key",
            typesafe_api_key="test-key",
            owner_token=uuid4(),
            observed_at=now,
            max_items=1,
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=2),
            enable_ats_enrichment=False,
        )
        stored = connection.execute(
            """
            SELECT d.outcome, d.decision_stage, d.matched_profile, w.state,
                   w.terminal_decision_id = d.id, d.reason LIKE %s
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE s.raw_url = %s
            """,
            ("Jev atomic policy failed:%", raw_url),
        ).fetchone()
        model_attempts = connection.execute(
            """
            SELECT provider, status, count(*)
            FROM model_call_attempts
            GROUP BY provider, status
            """
        ).fetchall()
        evaluated_criteria = connection.execute(
            "SELECT count(DISTINCT operation_key) FROM model_call_attempts"
        ).fetchone()

    assert summary.terminal_count == 1
    assert stored == ("rejected", "evaluation", None, "completed", True, True)
    assert model_attempts == [("typesafe", "accepted", 6)]
    assert evaluated_criteria == (6,)


def _prepare_run(connection: psycopg.Connection[tuple[object, ...]], key: str, now: datetime):
    return prepare_orchestration_run(
        connection,
        idempotency_key=key,
        implementation_ref="commit-1",
        started_at=now,
        load_active_configuration=load_published_active_search_configuration,
        fetch_rates=lambda: ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.11")},
            source="frankfurter",
            observed_at=now,
        ),
    )


def _model_response(tool_name: str, output: Mapping[str, object]) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "id": f"generation-{tool_name}",
                "model": "google/gemini-2.5-flash-001",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(output),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "cost": 0.00012,
                },
            }
        ),
    )


def _jev_response(body: dict[str, object], probability: float) -> JevHttpResponse:
    questions = cast(dict[str, object], body["questions"])
    return JevHttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "model": JEV_MODEL,
                "answers": {name: {"type": "noul", "noul": probability} for name in questions},
                "usage": {"input_tokens": 12, "output_tokens": len(questions)},
            }
        ),
    )


def _rejecting_jev_call(
    _url: str,
    _headers: Mapping[str, str],
    body: dict[str, object],
    _timeout: float,
) -> JevHttpResponse:
    return _jev_response(body, 1.0)


def _qualifying_jev_call(
    _url: str,
    _headers: Mapping[str, str],
    body: dict[str, object],
    _timeout: float,
) -> JevHttpResponse:
    questions = cast(dict[str, object], body["questions"])
    probabilities = dict.fromkeys(questions, 0.0)
    if "owns_product_delivery" in probabilities:
        probabilities["owns_product_delivery"] = 1.0
    return JevHttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "model": JEV_MODEL,
                "answers": {
                    name: {"type": "noul", "noul": probability}
                    for name, probability in probabilities.items()
                },
                "usage": {"input_tokens": 12, "output_tokens": len(questions)},
            }
        ),
    )


def _signal_free_jev_call(
    _url: str,
    _headers: Mapping[str, str],
    body: dict[str, object],
    _timeout: float,
) -> JevHttpResponse:
    return _jev_response(body, 0.0)


def _retryable_jev_call(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> JevHttpResponse:
    return JevHttpResponse(status_code=503, body="{}")


def _terminal_jev_call(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> JevHttpResponse:
    return JevHttpResponse(status_code=400, body="{}")


def _unexpected_jev_call(*_args: object) -> JevHttpResponse:
    raise AssertionError("rejected work reached Jev")


def _unexpected_model_call(*_args: object) -> HttpResponse:
    raise AssertionError("rejected work reached OpenRouter")


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection
