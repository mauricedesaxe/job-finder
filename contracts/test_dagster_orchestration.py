from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.dagster import defs
from job_finder.ats.models import AtsAvailable
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import ScrapeSucceeded, SearchSucceeded
from job_finder.evaluation.openrouter import HttpResponse, RetryPolicy
from job_finder.evaluation.prompt_releases import bootstrap_prompt_release
from job_finder.pipeline.orchestration import PipelineBoundaries, process_claimed_jobs
from job_finder.pipeline.state import (
    claim_next_job,
    fail_job_claim,
    fail_orchestration_run,
    prepare_orchestration_run,
    register_discoveries,
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


def test_run_retry_reuses_frozen_prompt_release_and_exchange_rates(
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
            load_prompt_release=bootstrap_prompt_release,
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
            load_prompt_release=lambda _connection: pytest.fail("release was reloaded"),
            fetch_rates=lambda: pytest.fail("rates were refetched"),
        )

    assert second.id == first.id
    assert second.status == "running"
    assert second.exchange_rates == rates


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
            load_prompt_release=bootstrap_prompt_release,
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
            load_prompt_release=bootstrap_prompt_release,
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
        assert fail_job_claim(
            connection,
            claim,
            failed_at=now,
            retry_after=timedelta(minutes=2),
            error_code="reader_unavailable",
            reason="timeout",
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
    monkeypatch.setenv("JINA_API_KEY", "unused")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_REF", "commit-1")

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
            SELECT status, implementation_ref, prompt_release_id IS NOT NULL
            FROM pipeline_runs
            WHERE kind = 'orchestration'
            """
        ).fetchone()
    assert stored == ("completed", "commit-1", True)


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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product."
                ),
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

    assert summary.terminal_count == 1
    assert stored == ("rejected", "ats_structural", "completed", True)


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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product."
                ),
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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product remotely."
                ),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_rejecting_model_call,
            ),
            openrouter_api_key="test-key",
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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product remotely."
                ),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_retryable_model_call,
                model_retry_policy=one_attempt,
            ),
            openrouter_api_key="test-key",
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
                *(("evaluate_job", {"pass": True, "reason": "filter passed"}) for _ in range(4)),
                ("evaluate_job", {"pass": True, "reason": "profile matched"}),
                ("evaluate_job", {"pass": False, "reason": "other profile"}),
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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product remotely."
                ),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=accepted_sender,
                model_retry_policy=one_attempt,
            ),
            openrouter_api_key="test-key",
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
                        scrape=lambda _url: ScrapeSucceeded(
                            markdown="# Senior Product Engineer\nBuild remotely."
                        ),
                        fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                        model_sender=_terminal_model_call,
                    ),
                    openrouter_api_key="test-key",
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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild remotely."
                ),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_unexpected_model_call,
            ),
            openrouter_api_key="test-key",
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


def test_terminal_openrouter_error_dead_letters_work_without_a_decision(
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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product remotely."
                ),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=_terminal_model_call,
            ),
            openrouter_api_key="test-key",
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
            *(("evaluate_job", {"pass": True, "reason": "filter passed"}) for _ in range(4)),
            ("evaluate_job", {"pass": True, "reason": "profile matched"}),
            ("evaluate_job", {"pass": False, "reason": "other profile"}),
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
                scrape=lambda _url: ScrapeSucceeded(
                    markdown="# Senior Product Engineer\nBuild the product remotely."
                ),
                fetch_ats=lambda _url, _title: pytest.fail("ATS should be disabled"),
                model_sender=model_sender,
            ),
            openrouter_api_key="test-key",
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


def _prepare_run(connection: psycopg.Connection[tuple[object, ...]], key: str, now: datetime):
    return prepare_orchestration_run(
        connection,
        idempotency_key=key,
        implementation_ref="commit-1",
        started_at=now,
        load_prompt_release=bootstrap_prompt_release,
        fetch_rates=lambda: ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.11")},
            source="frankfurter",
            observed_at=now,
        ),
    )


def _retryable_model_call(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> HttpResponse:
    return HttpResponse(
        status_code=503,
        body=json.dumps({"error": {"message": "provider unavailable"}}),
    )


def _terminal_model_call(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> HttpResponse:
    return HttpResponse(
        status_code=400,
        body=json.dumps({"error": {"message": "invalid request"}}),
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


def _rejecting_model_call(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "id": "generation-rejected",
                "model": "google/gemini-2.5-flash-001",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "evaluate_job",
                                        "arguments": json.dumps(
                                            {"pass": False, "reason": "wrong role"}
                                        ),
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
