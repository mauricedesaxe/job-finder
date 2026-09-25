from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
import requests
from psycopg.conninfo import make_conninfo
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.ats.models import AtsAvailable
from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ConfigurationActivated,
    activate_search_configuration,
)
from job_finder.database import apply_migrations
from job_finder.dagster import defs
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import ScrapeSucceeded, SearchSucceeded
from job_finder.evaluation.models import ReleaseTarget
from job_finder.evaluation.openrouter import HttpResponse
from job_finder.evaluation.prompt_releases import (
    build_prompt_release,
    store_prompt_release,
)
from job_finder.evaluation.release_targets import get_active_release_target
from job_finder.execution_budget import (
    BudgetSaved,
    ExecutionBlocked,
    postgres_budget_setup_service,
)
from job_finder.jobs.decision_pipeline import job_id_for_url
from job_finder.onboarding_test_search import (
    CreateOnboardingTestSearch,
    OnboardingTestSearchAccepted,
    OnboardingProviderAttemptLimit,
    OnboardingProviderOutcomeUnknown,
    claim_next_onboarding_test_search,
    complete_onboarding_test_search,
    create_onboarding_test_search,
    fail_onboarding_test_search,
    finish_onboarding_search_query,
    finish_onboarding_provider_dispatch,
    load_onboarding_test_search,
    prepare_onboarding_provider_dispatch,
    reserve_onboarding_search_query,
)
from job_finder.onboarding_test_search_worker import (
    execute_next_onboarding_test_search,
    guard_onboarding_provider_boundaries,
)
from job_finder.pipeline.orchestration import PipelineBoundaries
from job_finder.pipeline.work_items import JobWorkClaim, claim_next_job
from job_finder.pipeline.runs import prepare_onboarding_run, prepare_orchestration_run
from job_finder.pipeline.discoveries import register_discoveries
from job_finder.review.owner_access import OwnerBootstrapped, postgres_owner_access_service
from job_finder.search_configuration import (
    build_search_configuration_revision,
    build_search_queries,
    load_active_search_configuration,
    load_search_configuration_revision,
    store_search_configuration_revision,
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_onboarding_search_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_concurrent_same_key_submissions_produce_one_request(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    barrier = Barrier(2)

    def submit(_index: int) -> object:
        _ = barrier.wait()
        with _connection(authority_schema) as connection:
            return create_onboarding_test_search(
                connection,
                CreateOnboardingTestSearch(
                    idempotency_key="owner-setup",
                    actor="owner",
                    timestamp=now,
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(submit, range(2)))

    accepted = [result for result in results if isinstance(result, OnboardingTestSearchAccepted)]
    assert len(accepted) == 2
    assert sum(result.replayed for result in accepted) == 1
    assert accepted[0].request.run_id == accepted[1].request.run_id
    with _connection(authority_schema) as connection:
        count = connection.execute(
            "SELECT count(*) FROM onboarding_test_search_requests"
        ).fetchone()
        assert count == (1,)
        reservations = connection.execute(
            "SELECT count(*) FROM execution_budget_reservations"
        ).fetchone()
        assert reservations == (1,)


def test_onboarding_work_cannot_enter_the_production_claim_queue(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    _prepare_test_search_owner(authority_schema, now)
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        request = created.request
        onboarding_run = prepare_onboarding_run(
            connection,
            run_id=request.run_id,
            request_key=request.idempotency_key,
            implementation_ref="commit-1",
            configuration_revision_id=request.configuration_revision_id,
            target=ReleaseTarget(
                prompt_release_id=request.prompt_release_id,
                relevance_release_id=request.relevance_release_id,
            ),
            started_at=now,
            fetch_rates=lambda: rates,
        )
        production_run = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:scope-test",
            implementation_ref="commit-1",
            configuration_revision_id=request.configuration_revision_id,
            target=ReleaseTarget(
                prompt_release_id=request.prompt_release_id,
                relevance_release_id=request.relevance_release_id,
            ),
            started_at=now,
            fetch_rates=lambda: rates,
        )
        _ = register_discoveries(
            connection,
            run_id=onboarding_run.id,
            keyword="product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=("https://jobs.ashbyhq.com/acme/onboarding",),
            discovered_at=now,
            onboarding_request_key=request.idempotency_key,
        )
        _ = register_discoveries(
            connection,
            run_id=production_run.id,
            keyword="product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=("https://jobs.ashbyhq.com/acme/production",),
            discovered_at=now,
        )
        production_claim = claim_next_job(
            connection, owner_token=uuid4(), claimed_at=now, lease_for=timedelta(minutes=5)
        )
        onboarding_claim = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=now,
            lease_for=timedelta(minutes=5),
            onboarding_request_key=request.idempotency_key,
        )

    assert production_claim is not None
    assert production_claim.raw_url.endswith("/production")
    assert onboarding_claim is not None
    assert onboarding_claim.raw_url.endswith("/onboarding")


def test_worker_completes_pinned_empty_search_once(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    _prepare_test_search_owner(authority_schema, now)
    queries: list[tuple[str, str]] = []

    def search(keyword: str, domain: str) -> SearchSucceeded:
        queries.append((keyword, domain))
        return SearchSucceeded(urls=())

    boundaries = PipelineBoundaries(
        search=search,
        scrape=lambda _url: pytest.fail("Empty search must not scrape"),
        fetch_ats=lambda _url, _title: pytest.fail("Empty search must not fetch ATS"),
    )
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        result = execute_next_onboarding_test_search(
            connection,
            boundaries,
            implementation_ref="commit-1",
            openrouter_api_key="unused",
            typesafe_api_key=None,
            owner_token=uuid4(),
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=1),
            enable_ats_enrichment=False,
            fetch_rates=lambda: rates,
            now=lambda: now,
        )
        replay = execute_next_onboarding_test_search(
            connection,
            boundaries,
            implementation_ref="commit-1",
            openrouter_api_key="unused",
            typesafe_api_key=None,
            owner_token=uuid4(),
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=1),
            enable_ats_enrichment=False,
            fetch_rates=lambda: pytest.fail("Completed run must not refetch rates"),
            now=lambda: now,
        )
        reservation = connection.execute(
            """
            SELECT status, consumed_usd FROM execution_budget_reservations
            WHERE idempotency_key = %s
            """,
            (created.request.budget_reservation_key,),
        ).fetchone()

    assert result.state == "completed"
    assert result.queries == created.request.limits.max_queries
    assert result.jobs == 0
    assert len(queries) == created.request.limits.max_queries
    assert replay.state == "idle"
    assert reservation == ("settled", Decimal("50"))


def test_dagster_worker_runs_a_pending_onboarding_request(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
    settings = PostgresContractSettings.from_environment()
    monkeypatch.setenv(
        "JOB_FINDER_POSTGRES_DSN",
        make_conninfo(settings.postgres_dsn, options=f"-c search_path={authority_schema}"),
    )
    monkeypatch.setenv("JINA_API_KEY", "unused")
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unused")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_REF", "commit-1")

    def fixed_boundaries(*, jina_api_key: str) -> PipelineBoundaries:
        assert jina_api_key == "unused"
        return PipelineBoundaries(
            search=lambda _keyword, _domain: SearchSucceeded(urls=()),
            scrape=lambda _url: pytest.fail("Empty search must not scrape"),
            fetch_ats=lambda _url, _title: pytest.fail("Empty search must not fetch ATS"),
        )

    def fixed_rates(observed_at: datetime) -> ExchangeRateSnapshot:
        return ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=observed_at
        )

    monkeypatch.setattr(
        "job_finder.dagster.production_boundaries",
        fixed_boundaries,
    )
    monkeypatch.setattr("job_finder.dagster._fetch_rates", fixed_rates)

    execution = defs.resolve_job_def("onboarding_test_search").execute_in_process()

    assert execution.success
    with _connection(authority_schema) as connection:
        stored = load_onboarding_test_search(connection, "owner-setup")
    assert stored is not None
    assert stored.state == "completed"


def test_worker_processes_only_its_scoped_job(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    _prepare_test_search_owner(authority_schema, now)
    test_url = "https://jobs.ashbyhq.com/acme/onboarding-onsite"
    production_url = "https://jobs.ashbyhq.com/acme/production-backlog"
    calls = 0

    def search(_keyword: str, _domain: str) -> SearchSucceeded:
        nonlocal calls
        calls += 1
        return SearchSucceeded(urls=(test_url,) if calls == 1 else ())

    def no_model_call(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        pytest.fail("ATS rejection must not call a model")

    boundaries = PipelineBoundaries(
        search=search,
        scrape=lambda _url: ScrapeSucceeded(
            markdown="# Senior Product Engineer\n" + "Build a product with a remote team. " * 20
        ),
        fetch_ats=lambda _url, _title: AtsAvailable(
            source="ashby",
            location="London",
            locations=("London",),
            workplace_type="OnSite",
            country="GB",
        ),
        model_sender=no_model_call,
    )
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        production_run = prepare_orchestration_run(
            connection,
            idempotency_key="dagster:production-backlog",
            implementation_ref="commit-1",
            configuration_revision_id=created.request.configuration_revision_id,
            target=ReleaseTarget(
                prompt_release_id=created.request.prompt_release_id,
                relevance_release_id=created.request.relevance_release_id,
            ),
            started_at=now,
            fetch_rates=lambda: rates,
        )
        _ = register_discoveries(
            connection,
            run_id=production_run.id,
            keyword="product engineer",
            domain="jobs.ashbyhq.com",
            raw_urls=(production_url,),
            discovered_at=now,
        )
        result = execute_next_onboarding_test_search(
            connection,
            boundaries,
            implementation_ref="commit-1",
            openrouter_api_key="unused",
            typesafe_api_key="unused",
            owner_token=uuid4(),
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=1),
            enable_ats_enrichment=True,
            fetch_rates=lambda: rates,
            now=lambda: now,
        )
        rows = connection.execute(
            """
            SELECT jobs.raw_url, work.state
            FROM job_work_items work JOIN jobs ON jobs.id = work.job_id
            ORDER BY jobs.raw_url
            """
        ).fetchall()

    assert result.state == "completed"
    assert result.jobs == 1
    assert rows == [(test_url, "completed"), (production_url, "pending")]


def test_provider_dispatch_replays_response_and_stops_unknown_outcome(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    owner_token = uuid4()
    raw_url = "https://jobs.ashbyhq.com/acme/provider-receipt"
    job_id = job_id_for_url(raw_url)
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        claimed = claim_next_onboarding_test_search(
            connection,
            owner_token=owner_token,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claimed is not None
        _ = connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, %s, %s, %s)
            """,
            (job_id, raw_url, now, now),
        )
        generation_calls = 0

        def get_generation(
            _url: str,
            _headers: Mapping[str, str],
            _provider_response_id: str,
            _timeout: float,
        ) -> HttpResponse:
            nonlocal generation_calls
            generation_calls += 1
            if generation_calls == 1:
                raise requests.Timeout("temporary generation lookup failure")
            return HttpResponse(
                status_code=200,
                body='{"data":{"tokens_prompt":1,"tokens_completion":1,"total_cost":0.01}}',
            )

        guarded, select_claim = guard_onboarding_provider_boundaries(
            connection,
            PipelineBoundaries(
                search=lambda _keyword, _domain: pytest.fail("search was called"),
                scrape=lambda _url: pytest.fail("scrape was called"),
                fetch_ats=lambda _url, _title: pytest.fail("ATS was called"),
                generation_sender=get_generation,
            ),
            claimed,
            owner_token,
            lambda: now,
        )
        select_claim(
            JobWorkClaim(
                job_id=job_id,
                raw_url=raw_url,
                keyword="provider receipt",
                owner_token=owner_token,
                attempt_count=1,
                lease_expires_at=now + timedelta(minutes=5),
            )
        )
        assert guarded.model_call_started is not None
        assert guarded.generation_sender is not None
        guarded.model_call_started("evaluation:criterion")
        with pytest.raises(requests.Timeout):
            guarded.generation_sender("https://openrouter.test", {}, "generation-1", 30)
        generation_response = guarded.generation_sender(
            "https://openrouter.test", {}, "generation-1", 30
        )
        first = prepare_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            owner_token=owner_token,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="a" * 64,
            attempted_at=now,
        )
        finish_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="a" * 64,
            attempt_number=first.attempt_number,
            status_code=200,
            response_body='{"id":"accepted"}',
        )
        replay = prepare_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            owner_token=owner_token,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="a" * 64,
            attempted_at=now,
        )
        distinct_operation = prepare_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            owner_token=owner_token,
            job_id=job_id,
            operation_key="enrichment",
            provider="openrouter",
            body_digest="a" * 64,
            attempted_at=now,
        )
        _ = prepare_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            owner_token=owner_token,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="b" * 64,
            attempted_at=now,
        )
        generation_first = prepare_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            owner_token=owner_token,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="d" * 64,
            attempted_at=now,
            retryable_statuses=frozenset({404, 429}),
        )
        finish_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="d" * 64,
            attempt_number=generation_first.attempt_number,
            status_code=404,
            response_body='{"error":"generation pending"}',
        )
        generation_retry = prepare_onboarding_provider_dispatch(
            connection,
            request_key=claimed.idempotency_key,
            owner_token=owner_token,
            job_id=job_id,
            operation_key="evaluation:criterion",
            provider="openrouter",
            body_digest="d" * 64,
            attempted_at=now,
            retryable_statuses=frozenset({404, 429}),
        )
        with pytest.raises(OnboardingProviderOutcomeUnknown):
            _ = prepare_onboarding_provider_dispatch(
                connection,
                request_key=claimed.idempotency_key,
                owner_token=owner_token,
                job_id=job_id,
                operation_key="evaluation:criterion",
                provider="openrouter",
                body_digest="b" * 64,
                attempted_at=now,
            )
        _ = connection.execute(
            """
            UPDATE onboarding_test_search_requests
            SET provider_attempt_count = max_provider_attempts
            WHERE idempotency_key = %s
            """,
            (claimed.idempotency_key,),
        )
        with pytest.raises(OnboardingProviderAttemptLimit):
            _ = prepare_onboarding_provider_dispatch(
                connection,
                request_key=claimed.idempotency_key,
                owner_token=owner_token,
                job_id=job_id,
                operation_key="evaluation:criterion",
                provider="openrouter",
                body_digest="c" * 64,
                attempted_at=now,
            )

    assert first.cached_status_code is None
    assert generation_calls == 2
    assert generation_response.status_code == 200
    assert replay.cached_status_code == 200
    assert replay.cached_body == '{"id":"accepted"}'
    assert distinct_operation.cached_status_code is None
    assert generation_retry.attempt_number == 2
    assert generation_retry.cached_status_code is None


def test_worker_recovers_without_repeating_a_reserved_search_query(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    resumed_at = now + timedelta(minutes=6)
    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    _prepare_test_search_owner(authority_schema, now)
    searched: list[tuple[str, str]] = []

    def search(keyword: str, domain: str) -> SearchSucceeded:
        searched.append((keyword, domain))
        return SearchSucceeded(urls=())

    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        request = created.request
        first_owner = uuid4()
        claimed = claim_next_onboarding_test_search(
            connection,
            owner_token=first_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claimed is not None
        queries = build_search_queries(
            load_search_configuration_revision(
                connection, request.configuration_revision_id
            ).configuration
        )
        assert reserve_onboarding_search_query(
            connection,
            request=request,
            owner_token=first_owner,
            ordinal=0,
            query=queries[0],
            reserved_at=now,
        )
        result = execute_next_onboarding_test_search(
            connection,
            PipelineBoundaries(
                search=search,
                scrape=lambda _url: pytest.fail("Empty search must not scrape"),
                fetch_ats=lambda _url, _title: pytest.fail("Empty search must not fetch ATS"),
            ),
            implementation_ref="commit-1",
            openrouter_api_key="unused",
            typesafe_api_key=None,
            owner_token=uuid4(),
            lease_for=timedelta(minutes=5),
            retry_after=timedelta(minutes=1),
            enable_ats_enrichment=False,
            fetch_rates=lambda: rates,
            now=lambda: resumed_at,
        )
        first_receipt = connection.execute(
            """
            SELECT state FROM onboarding_search_queries
            WHERE request_key = %s AND ordinal = 0
            """,
            (request.idempotency_key,),
        ).fetchone()

    assert result.state == "completed"
    assert len(searched) == len(queries) - 1
    assert (queries[0].keyword, queries[0].domain) not in searched
    assert first_receipt == ("unavailable",)


@pytest.mark.parametrize("preexisting", [False, True])
def test_query_registration_obeys_job_and_url_limits(
    authority_schema: str, preexisting: bool
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    rates = ExchangeRateSnapshot(
        rates={"EUR": Decimal("1.11")}, source="frankfurter", observed_at=now
    )
    _prepare_test_search_owner(authority_schema, now)
    urls = tuple(f"https://jobs.ashbyhq.com/acme/bounded-{index}" for index in range(100))
    owner_token = uuid4()
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(idempotency_key="owner-setup", actor="owner", timestamp=now),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        request = created.request
        claimed = claim_next_onboarding_test_search(
            connection, owner_token=owner_token, claimed_at=now, lease_for=timedelta(minutes=5)
        )
        assert claimed is not None
        _ = prepare_onboarding_run(
            connection,
            run_id=request.run_id,
            request_key=request.idempotency_key,
            implementation_ref="commit-1",
            configuration_revision_id=request.configuration_revision_id,
            target=ReleaseTarget(
                prompt_release_id=request.prompt_release_id,
                relevance_release_id=request.relevance_release_id,
            ),
            started_at=now,
            fetch_rates=lambda: rates,
        )
        if preexisting:
            for raw_url in urls:
                _ = connection.execute(
                    """
                    INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (job_id_for_url(raw_url), raw_url, now, now),
                )
        queries = build_search_queries(
            load_search_configuration_revision(
                connection, request.configuration_revision_id
            ).configuration
        )
        for ordinal in range(2):
            assert reserve_onboarding_search_query(
                connection,
                request=request,
                owner_token=owner_token,
                ordinal=ordinal,
                query=queries[ordinal],
                reserved_at=now,
            )
            _ = finish_onboarding_search_query(
                connection,
                request=request,
                owner_token=owner_token,
                ordinal=ordinal,
                query=queries[ordinal],
                raw_urls=urls,
                finished_at=now,
            )
        totals = connection.execute(
            """
            SELECT sum(url_count), sum(new_work_count)
            FROM onboarding_search_queries WHERE request_key = %s
            """,
            (request.idempotency_key,),
        ).fetchone()

    assert totals == ((40, 0) if preexisting else (10, 10))


def test_replay_keeps_pinned_provenance_after_active_config_changes(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    command = CreateOnboardingTestSearch(
        idempotency_key="owner-setup",
        actor="owner",
        timestamp=now,
    )
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(connection, command)
        assert isinstance(created, OnboardingTestSearchAccepted)
        assert created.replayed is False
        original = created.request
        assert original.limits.max_queries == 128
        assert original.limits.max_urls == 40
        assert original.limits.max_jobs == 10
        assert original.limits.max_work_attempts == 3
        assert original.limits.max_provider_attempts == 400
        assert original.limits.run_allowance_usd == Decimal("50")
        _ = connection.execute(
            """
            UPDATE active_search_configuration
            SET generation = generation + 1, activated_at = %s, activated_by = 'later'
            WHERE singleton_id = 1
            """,
            (now + timedelta(hours=1),),
        )
        _ = connection.execute(
            """
            UPDATE execution_budget_policy
            SET version = version + 1, monthly_limit_usd = 900, updated_at = %s,
                updated_by = 'later'
            WHERE singleton_id = 1
            """,
            (now + timedelta(hours=1),),
        )
        replayed = create_onboarding_test_search(connection, command)

    assert isinstance(replayed, OnboardingTestSearchAccepted)
    assert replayed.replayed is True
    assert replayed.request.run_id == original.run_id
    assert replayed.request.configuration_revision_id == original.configuration_revision_id
    assert replayed.request.prompt_release_id == original.prompt_release_id
    assert replayed.request.relevance_release_id == original.relevance_release_id
    assert replayed.request.release_generation == original.release_generation
    assert replayed.request.budget_policy_version == original.budget_policy_version
    assert replayed.request.limits == original.limits


def test_request_uses_acquisition_queries_and_release_target_model_bounds(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    with _connection(authority_schema) as connection:
        initial = load_active_search_configuration(connection)
        target = get_active_release_target(connection)
        acquisition = initial.revision.configuration.model_copy(
            update={
                "personal_criteria": initial.revision.configuration.personal_criteria[:1],
                "target_profiles": initial.revision.configuration.target_profiles[:1],
            }
        )
        revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                acquisition,
                created_at=now + timedelta(minutes=1),
                created_by="owner",
            ),
        )
        publication_release = store_prompt_release(
            connection,
            build_prompt_release(acquisition),
            created_at=now + timedelta(minutes=1),
            created_by="owner",
        )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, 'owner')
            """,
            (revision.id, publication_release.id, now + timedelta(minutes=1)),
        )
        activated = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=revision.id,
                expected_active_revision_id=initial.revision.id,
                expected_generation=initial.generation,
                actor="owner",
                timestamp=now + timedelta(minutes=2),
            ),
        )
        assert isinstance(activated, ConfigurationActivated)

        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="release-target-budget",
                actor="owner",
                timestamp=now + timedelta(minutes=3),
            ),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        reservation = connection.execute(
            """
            SELECT configuration_revision_id, prompt_release_id, relevance_release_id,
                   release_generation, search_queries, logical_model_calls_per_job,
                   maximum_provider_attempts
            FROM execution_budget_reservations
            WHERE idempotency_key = %s
            """,
            (created.request.budget_reservation_key,),
        ).fetchone()

    assert created.request.configuration_revision_id == revision.id
    assert created.request.prompt_release_id == target.target.prompt_release_id
    assert created.request.relevance_release_id == target.target.relevance_release_id
    assert created.request.limits.max_queries == len(build_search_queries(acquisition))
    assert created.request.limits.max_provider_attempts == 400
    assert reservation == (
        revision.id,
        target.target.prompt_release_id,
        target.target.relevance_release_id,
        target.generation,
        len(build_search_queries(acquisition)),
        8,
        400,
    )


def test_leases_are_exclusive_reclaimable_and_reject_stale_owners(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    first_owner = uuid4()
    second_owner = uuid4()
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        claimed = claim_next_onboarding_test_search(
            connection,
            owner_token=first_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claimed is not None
        assert claimed.state == "leased"
        assert claimed.owner_token == first_owner
        assert claimed.attempt_count == 1
        concurrent = claim_next_onboarding_test_search(
            connection,
            owner_token=second_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert concurrent is None
        stale = complete_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=second_owner,
            completed_at=now + timedelta(seconds=1),
        )
        assert stale is None
        reclaimed = claim_next_onboarding_test_search(
            connection,
            owner_token=second_owner,
            claimed_at=now + timedelta(minutes=6),
            lease_for=timedelta(minutes=5),
        )
        assert reclaimed is not None
        assert reclaimed.owner_token == second_owner
        assert reclaimed.attempt_count == 2
        lost = complete_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=first_owner,
            completed_at=now + timedelta(minutes=6),
        )
        assert lost is None
        finished = complete_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=second_owner,
            completed_at=now + timedelta(minutes=6),
        )
        assert finished is not None
        assert finished.state == "completed"
        with pytest.raises(psycopg.errors.CheckViolation, match="terminal"):
            connection.execute(
                """
                UPDATE onboarding_test_search_requests
                SET updated_at = %s
                WHERE idempotency_key = 'owner-setup'
                """,
                (now + timedelta(minutes=7),),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM onboarding_test_search_requests WHERE idempotency_key = 'owner-setup'"
            )


def test_worker_failure_fails_the_request_and_rejects_late_failures(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    owner = uuid4()
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        with pytest.raises(ValueError, match="lease must be positive"):
            claim_next_onboarding_test_search(
                connection,
                owner_token=owner,
                claimed_at=now,
                lease_for=timedelta(0),
            )
        claimed = claim_next_onboarding_test_search(
            connection,
            owner_token=owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claimed is not None
        assert claimed.state == "leased"
        failed = fail_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=owner,
            completed_at=now + timedelta(minutes=1),
            error_code="provider_outage",
            error_reason="Upstream provider returned 503 for every retry",
        )
        assert failed is not None
        assert failed.state == "failed"
        assert failed.error_code == "provider_outage"
        assert failed.error_reason == "Upstream provider returned 503 for every retry"
        assert failed.completed_at == now + timedelta(minutes=1)
        stored = load_onboarding_test_search(connection, "owner-setup")
        assert stored is not None
        assert stored.state == "failed"
        assert stored.error_code == "provider_outage"
        assert stored.error_reason == "Upstream provider returned 503 for every retry"
        assert stored.completed_at == now + timedelta(minutes=1)
        assert (
            claim_next_onboarding_test_search(
                connection,
                owner_token=uuid4(),
                claimed_at=now + timedelta(minutes=10),
                lease_for=timedelta(minutes=5),
            )
            is None
        )
        assert (
            fail_onboarding_test_search(
                connection,
                run_id=created.request.run_id,
                owner_token=owner,
                completed_at=now + timedelta(minutes=2),
                error_code="late_failure",
                error_reason="Worker reported after the request became terminal",
            )
            is None
        )
        unchanged = load_onboarding_test_search(connection, "owner-setup")
        assert unchanged is not None
        assert unchanged.state == "failed"
        assert unchanged.error_code == "provider_outage"


def test_attempt_exhaustion_fails_the_request(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        claimed_at = now
        for _ in range(created.request.limits.max_work_attempts):
            owner = uuid4()
            claimed = claim_next_onboarding_test_search(
                connection,
                owner_token=owner,
                claimed_at=claimed_at,
                lease_for=timedelta(minutes=1),
            )
            assert claimed is not None
            claimed_at = claimed_at + timedelta(minutes=2)
        exhausted = claim_next_onboarding_test_search(
            connection,
            owner_token=uuid4(),
            claimed_at=claimed_at,
            lease_for=timedelta(minutes=1),
        )
        assert exhausted is None
        stored = load_onboarding_test_search(connection, "owner-setup")
        assert stored is not None
        assert stored.state == "failed"
        assert stored.error_code == "worker_attempts_exhausted"
        reservation = connection.execute(
            """
            SELECT status, consumed_usd FROM execution_budget_reservations
            WHERE idempotency_key = %s
            """,
            (created.request.budget_reservation_key,),
        ).fetchone()
        assert reservation == ("settled", Decimal("0"))


def test_create_is_blocked_before_test_search_stage(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        blocked = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
    assert blocked == ExecutionBlocked(reason="onboarding_incomplete")


def _prepare_test_search_owner(schema_name: str, timestamp: datetime) -> None:
    with _connection(schema_name) as connection:
        apply_migrations(connection)
        _ = load_active_search_configuration(connection)
    owner = postgres_owner_access_service(lambda: _connection(schema_name))
    bootstrapped = owner.bootstrap("first secure owner password")
    assert isinstance(bootstrapped, OwnerBootstrapped)
    with _connection(schema_name) as connection:
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'preferences', updated_at = %s
            WHERE singleton_id = 1
            """,
            (timestamp,),
        )
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'budget', updated_at = %s
            WHERE singleton_id = 1
            """,
            (timestamp,),
        )
    saved = postgres_budget_setup_service(lambda: _connection(schema_name)).save(
        0,
        Decimal("500"),
        Decimal("50"),
        10,
        "owner",
        timestamp,
    )
    assert isinstance(saved, BudgetSaved)


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection
