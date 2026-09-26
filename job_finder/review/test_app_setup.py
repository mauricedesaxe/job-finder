from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import re
from typing import Never
from uuid import UUID

import psycopg
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from job_finder.execution_budget import (
    BudgetSaved,
    BudgetSetupService,
    BudgetSetupState,
    ExecutionBudgetPolicy,
    ExecutionEstimate,
)
from job_finder.evaluation.relevance_releases import RelevanceReleaseError
from job_finder.review.onboarding import (
    OnboardingSearchProgress,
    OnboardingSearchService,
    OnboardingSearchJob,
)
from job_finder.provider_credentials import (
    ProviderCapability,
    ProviderCredentialState,
    ProviderCredentialStored,
    ProviderKind,
    ProviderSetupService,
    ProviderSetupSnapshot,
    ProviderStageAdvanced,
)
from job_finder.web.app import create_review_app
from job_finder.pipeline.work_dismissals import (
    WorkDismissalCommand,
)
from job_finder.operations.service import OperationsService
from job_finder.review.owner_access import (
    OnboardingStage,
    OwnerAccessService,
    OwnerAccessState,
    OwnerBootstrapped,
)

from job_finder.review.test_app_support import (
    helper_default_submit_review as _default_submit_review,
    helper_test_search_request as _test_search_request,
    helper_test_search_client as _test_search_client,
    helper_client as _client,
    helper_authenticate as _authenticate,
    helper_queue as _queue,
    helper_csrf as _csrf,
    helper_operations_snapshot as _operations_snapshot,
    helper_activity_run_entry as _activity_run_entry,
    helper_activity_work_entry as _activity_work_entry,
    helper_activity_service as _activity_service,
    helper_applied_dismissal as _applied_dismissal,
    NOW,
    SETTINGS,
    OWNER_PASSWORD,
    BOOTSTRAP_TOKEN,
    OWNER_ACCESS,
)


@pytest.mark.parametrize(
    ("method", "path", "location"),
    [
        ("GET", "/review", "/login?next=%2Freview"),
        (
            "GET",
            f"/review/item/{UUID(int=1)}",
            f"/login?next=%2Freview%2Fitem%2F{UUID(int=1)}",
        ),
        (
            "POST",
            f"/review/{UUID(int=1)}",
            f"/login?next=%2Freview%2F{UUID(int=1)}",
        ),
    ],
)
def test_requires_a_signed_session_for_review_routes(method: str, path: str, location: str) -> None:
    def unused(*_args: object) -> Never:
        raise AssertionError("review services must not run before authentication")

    client = TestClient(
        create_review_app(
            unused,
            SETTINGS,
            submit_review=unused,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    response = client.request(method, path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == location


def test_fresh_install_redirects_to_one_time_owner_setup() -> None:
    state = [OwnerAccessState(stage=OnboardingStage.OWNER_ACCOUNT, has_password=False)]
    calls: list[str] = []

    def bootstrap(password: str) -> OwnerBootstrapped:
        calls.append(password)
        state[0] = OwnerAccessState(stage=OnboardingStage.PROVIDERS, has_password=True)
        return OwnerBootstrapped(state[0])

    owner_access = OwnerAccessService(
        load_state=lambda: state[0],
        authenticate=lambda _password: False,
        bootstrap=bootstrap,
    )

    def replace_provider(
        _provider: ProviderKind,
        _secret: SecretStr,
        _generation: int,
        _actor: str,
        _timestamp: datetime,
    ) -> Never:
        pytest.fail("provider credential was unexpectedly replaced")

    provider_setup = ProviderSetupService(
        inspect=lambda: ProviderSetupSnapshot(
            credentials=tuple(
                ProviderCredentialState(provider=provider, generation=0)
                for provider in ProviderKind
            )
        ),
        replace=replace_provider,
        advance=lambda: pytest.fail("provider setup unexpectedly advanced"),
        resolve=lambda _provider: pytest.fail("provider credential was unexpectedly resolved"),
    )
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=owner_access,
            provider_setup_service=provider_setup,
            now=lambda: NOW,
        )
    )

    protected = client.get("/review", follow_redirects=False)
    forbidden = client.post(
        "/setup",
        data={"password": OWNER_PASSWORD, "password_confirmation": OWNER_PASSWORD},
    )
    setup = client.get("/setup")
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', setup.text)
    assert csrf_match is not None
    rejected_token = client.post(
        "/setup",
        data={
            "csrf_token": csrf_match.group(1),
            "bootstrap_token": "incorrect-bootstrap-token-with-32-characters",
            "password": OWNER_PASSWORD,
            "password_confirmation": OWNER_PASSWORD,
        },
    )
    completed = client.post(
        "/setup",
        data={
            "csrf_token": csrf_match.group(1),
            "bootstrap_token": BOOTSTRAP_TOKEN,
            "password": OWNER_PASSWORD,
            "password_confirmation": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )

    assert protected.status_code == 303
    assert protected.headers["location"] == "/setup"
    assert forbidden.status_code == 403
    assert rejected_token.status_code == 401
    assert "Create the owner password" in setup.text
    assert completed.status_code == 303
    assert completed.headers["location"] == "/setup/providers"
    assert calls == [OWNER_PASSWORD]
    review = client.get("/review", follow_redirects=False)
    assert review.status_code == 303
    assert review.headers["location"] == "/setup/providers"
    assert "Connect the services" in client.get("/setup/providers").text


def test_provider_setup_never_echoes_credentials_and_advances_when_ready() -> None:
    state = [OwnerAccessState(stage=OnboardingStage.PROVIDERS, has_password=True)]
    stored: dict[ProviderKind, ProviderCredentialState] = {}

    def inspect() -> ProviderSetupSnapshot:
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
        _actor: str,
        _timestamp: datetime,
    ) -> ProviderCredentialStored:
        assert secret.get_secret_value() == "credential-that-must-not-be-rendered"
        credential = ProviderCredentialState(
            provider=provider,
            generation=expected_generation + 1,
            capabilities={
                ProviderKind.JINA: (
                    ProviderCapability.SEARCH,
                    ProviderCapability.SCRAPE,
                ),
                ProviderKind.OPENROUTER: (
                    ProviderCapability.STRUCTURED_GENERATION,
                    ProviderCapability.USAGE_COST,
                ),
                ProviderKind.TYPESAFE: (
                    ProviderCapability.RELEVANCE_EVALUATION,
                    ProviderCapability.USAGE_COST,
                ),
            }[provider],
            validated_at=NOW,
        )
        stored[provider] = credential
        return ProviderCredentialStored(state=credential)

    def advance() -> ProviderStageAdvanced:
        assert inspect().ready
        state[0] = OwnerAccessState(stage=OnboardingStage.PREFERENCES, has_password=True)
        return ProviderStageAdvanced(state=state[0])

    owner_access = OwnerAccessService(
        load_state=lambda: state[0],
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    providers = ProviderSetupService(
        inspect=inspect,
        replace=replace,
        advance=advance,
        resolve=lambda _provider: pytest.fail("provider credential was unexpectedly resolved"),
    )
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=owner_access,
            provider_setup_service=providers,
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    csrf_token = _csrf(client)

    for provider in ProviderKind:
        response = client.post(
            "/setup/providers",
            data={
                "csrf_token": csrf_token,
                "provider": provider.value,
                "expected_generation": "0",
                "credential": "credential-that-must-not-be-rendered",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "credential-that-must-not-be-rendered" not in response.text

    completed = client.post(
        "/setup/providers/continue",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert completed.status_code == 303
    assert completed.headers["location"] == "/configuration"
    assert client.get("/review", follow_redirects=False).headers["location"] == "/configuration"


def test_budget_setup_shows_bounds_and_advances_to_test_search() -> None:
    owner_state = [OwnerAccessState(stage=OnboardingStage.BUDGET, has_password=True)]
    estimate = ExecutionEstimate(
        search_queries=12,
        jobs_per_run=25,
        logical_model_calls_per_job=8,
        maximum_provider_attempts=1600,
    )

    def save_budget(
        expected_version: int,
        monthly_limit: Decimal,
        run_limit: Decimal,
        max_jobs: int,
        _actor: str,
        _timestamp: datetime,
    ) -> BudgetSaved:
        policy = ExecutionBudgetPolicy(
            version=expected_version + 1,
            monthly_limit_usd=monthly_limit,
            run_allowance_usd=run_limit,
            max_jobs_per_run=max_jobs,
            max_search_queries_per_run=estimate.search_queries,
            max_provider_attempts_per_run=estimate.maximum_provider_attempts,
        )
        owner_state[0] = OwnerAccessState(stage=OnboardingStage.TEST_SEARCH, has_password=True)
        return BudgetSaved(policy=policy, owner_state=owner_state[0])

    budget = BudgetSetupService(
        inspect=lambda _max_jobs: BudgetSetupState(policy=None, estimate=estimate),
        save=save_budget,
    )
    owner = OwnerAccessService(
        load_state=lambda: owner_state[0],
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=owner,
            budget_setup_service=budget,
            test_search_service=OnboardingSearchService(
                inspect=lambda: OnboardingSearchProgress(request=None),
                launch=lambda _actor, _now: pytest.fail("search was unexpectedly launched"),
            ),
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    page = client.get("/setup/budget")
    csrf_token = _csrf(client)

    completed = client.post(
        "/setup/budget",
        data={
            "csrf_token": csrf_token,
            "expected_version": "0",
            "monthly_limit_usd": "20.00",
            "run_allowance_usd": "2.00",
            "max_jobs_per_run": "25",
        },
        follow_redirects=False,
    )

    assert "12 searches per discovery run" in page.text
    assert "1600 provider attempts" in page.text
    assert completed.status_code == 303
    assert completed.headers["location"] == "/setup/test-search"
    assert "Ready for a bounded test search" in client.get("/setup/test-search").text


def test_test_search_launch_requires_csrf_and_polls_durable_progress() -> None:
    client, _, progress, launches = _test_search_client()
    ready = client.get("/setup/test-search")
    assert "Start test search" in ready.text
    assert client.post("/setup/test-search", data={"csrf_token": "wrong"}).status_code == 403
    assert launches == []

    started = client.post(
        "/setup/test-search",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert started.headers["location"] == "/setup/test-search"
    assert launches == [("owner", NOW)]
    queued = client.get("/setup/test-search")
    assert "Test search queued" in queued.text
    assert 'http-equiv="refresh" content="5;url=/setup/test-search"' in queued.text

    progress[0] = OnboardingSearchProgress(
        request=_test_search_request("leased"),
        queries_completed=2,
        urls_checked=5,
        jobs_found=1,
        jobs=(OnboardingSearchJob("Engineer", "Acme", "https://example.com/job"),),
    )
    running = client.get("/setup/test-search")
    assert "Test search running" in running.text
    assert "2 of 3 searches completed" in running.text
    assert "Engineer" in running.text
    assert "Acme" in running.text


def test_test_search_failure_can_retry_and_completed_results_remain_visible() -> None:
    client, owner_state, progress, launches = _test_search_client()
    progress[0] = OnboardingSearchProgress(request=_test_search_request("failed"))
    failed = client.get("/setup/test-search")
    assert "The provider did not respond" in failed.text
    assert "Retry test search" in failed.text
    assert 'http-equiv="refresh"' not in failed.text
    retried = client.post(
        "/setup/test-search",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert retried.status_code == 303
    assert len(launches) == 1

    progress[0] = OnboardingSearchProgress(
        request=_test_search_request("completed"),
        queries_completed=3,
        urls_checked=8,
        jobs_found=1,
        jobs=(OnboardingSearchJob("Engineer", "Acme", "https://example.com/job"),),
    )
    owner_state[0] = OwnerAccessState(stage=OnboardingStage.COMPLETE, has_password=True)
    done = client.get("/setup/test-search")
    assert "Test search complete" in done.text
    assert "Open review queue" in done.text
    assert "Engineer" in done.text
    assert 'http-equiv="refresh"' not in done.text


def test_completed_upgrade_without_a_budget_is_routed_to_budget_setup() -> None:
    estimate = ExecutionEstimate(
        search_queries=12,
        jobs_per_run=25,
        logical_model_calls_per_job=8,
        maximum_provider_attempts=1600,
    )

    def save_budget(
        _expected_version: int,
        _monthly_limit: Decimal,
        _run_allowance: Decimal,
        _max_jobs: int,
        _actor: str,
        _timestamp: datetime,
    ) -> Never:
        pytest.fail("budget was unexpectedly saved")

    budget = BudgetSetupService(
        inspect=lambda _max_jobs: BudgetSetupState(policy=None, estimate=estimate),
        save=save_budget,
    )
    owner = OwnerAccessService(
        load_state=lambda: OwnerAccessState(stage=OnboardingStage.COMPLETE, has_password=True),
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=owner,
            budget_setup_service=budget,
        )
    )
    _authenticate(client)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/budget"


@pytest.mark.parametrize(
    ("stage", "path"),
    [
        (OnboardingStage.BUDGET, "/setup/budget"),
        (OnboardingStage.COMPLETE, "/"),
    ],
)
@pytest.mark.parametrize(
    ("error", "message", "log_message"),
    [
        (
            RelevanceReleaseError("Relevance policy implementation artifacts do not match"),
            "Activate a release compatible with this deployment",
            "Budget inspection failed because the relevance release is invalid",
        ),
        (
            psycopg.OperationalError("secret-password"),
            "Budget database state could not be read",
            "Budget database inspection failed: OperationalError",
        ),
        (
            RuntimeError("secret-password"),
            "Budget state could not be loaded. Check the server logs.",
            "Budget inspection failed: RuntimeError",
        ),
    ],
)
def test_budget_inspection_failure_reports_the_cause_without_exposing_secrets(
    stage: OnboardingStage,
    path: str,
    error: RuntimeError | psycopg.Error,
    message: str,
    log_message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def inspect(_max_jobs: int) -> Never:
        raise error

    def save_budget(
        _expected_version: int,
        _monthly_limit: Decimal,
        _run_allowance: Decimal,
        _max_jobs: int,
        _actor: str,
        _timestamp: datetime,
    ) -> Never:
        pytest.fail("budget was unexpectedly saved")

    owner = OwnerAccessService(
        load_state=lambda: OwnerAccessState(stage=stage, has_password=True),
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=owner,
            budget_setup_service=BudgetSetupService(
                inspect=inspect,
                save=save_budget,
            ),
        )
    )
    _authenticate(client)

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 503
    assert message in response.text
    assert "database recovers" not in response.text
    assert "secret-password" not in response.text
    assert log_message in caplog.text
    if not isinstance(error, RelevanceReleaseError):
        assert "secret-password" not in caplog.text


def test_legacy_install_fails_closed_until_password_import() -> None:
    legacy = OwnerAccessService(
        load_state=lambda: OwnerAccessState(
            stage=OnboardingStage.LEGACY_OWNER_IMPORT, has_password=False
        ),
        authenticate=lambda _password: False,
        bootstrap=lambda _password: pytest.fail("legacy installation became claimable"),
    )
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=legacy,
            now=lambda: NOW,
        )
    )

    response = client.get("/setup")

    assert response.status_code == 503
    assert "Legacy owner import is required" in response.text


def test_authenticates_and_signs_out_the_owner() -> None:
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    rejected = client.post("/login", data={"password": "wrong password", "next": "/review"})
    accepted = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": "/review"},
        follow_redirects=False,
    )
    csrf_token = _csrf(client)
    signed_out = client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)

    assert rejected.status_code == 401
    assert "password is incorrect" in rejected.text
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/review"
    assert signed_out.status_code == 303
    assert client.get("/review", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "next_value", ["//evil.example", "https://evil.example", "/\\evil.example"]
)
def test_login_redirects_unsafe_next_targets_to_the_review_home(next_value: str) -> None:
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    response = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": next_value},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_exposes_public_health_and_database_readiness() -> None:
    readiness_calls = 0

    def ready() -> None:
        nonlocal readiness_calls
        readiness_calls += 1

    app = create_review_app(
        lambda: _queue(),
        SETTINGS,
        submit_review=_default_submit_review,
        owner_access_service=OWNER_ACCESS,
        readiness=ready,
        now=lambda: NOW,
    )
    client = TestClient(app)

    assert client.get("/healthz").text == "ok"
    assert client.get("/readyz").text == "ready"
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 204
    assert favicon.content == b""
    assert readiness_calls == 1


def test_reports_database_readiness_failure_without_authentication() -> None:
    def unavailable() -> None:
        raise psycopg.OperationalError("database down")

    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=OWNER_ACCESS,
            readiness=unavailable,
            now=lambda: NOW,
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.text == "database unavailable"


def test_reports_incompatible_release_readiness_without_authentication(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def incompatible() -> None:
        raise RelevanceReleaseError(
            "Relevance policy implementation artifacts do not match current source artifacts"
        )

    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=OWNER_ACCESS,
            readiness=incompatible,
            now=lambda: NOW,
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.text == "active release incompatible"
    assert "Relevance policy implementation artifacts" in caplog.text


def test_dismiss_undo_redirects_with_its_own_notice() -> None:
    client = _client(
        _queue(),
        operations=OperationsService(
            load=lambda: _operations_snapshot(),
            dismiss=lambda command: _applied_dismissal(command),
        ),
    )

    response = client.post(
        "/operations/dismiss",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=32)),
            "action": "undo_dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-000000000020?notice=dismiss-undone"
    )


def test_dismissal_requires_csrf_before_calling_the_service() -> None:
    calls: list[WorkDismissalCommand] = []
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        dismiss=lambda command: calls.append(command) or _applied_dismissal(command),
    )
    client = _client(_queue(), operations=operations)

    response = client.post(
        "/operations/dismiss",
        data={
            "job_id": str(UUID(int=32)),
            "action": "dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 403
    assert calls == []


def test_the_activity_page_renders_runs_work_and_pagination() -> None:
    activity, _ = _activity_service(
        _activity_run_entry(value=1, idle=True),
        _activity_run_entry(value=2, kind="discovery"),
        _activity_work_entry(value=9, state="failed"),
        cursor="next-cursor-token",
    )
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs")

    assert listing.status_code == 200
    assert "A run is one pipeline pass; a job is one listing being worked on." in listing.text
    assert "Scheduler tick" in listing.text
    assert "Nothing was due." in listing.text
    assert "Discovery run" in listing.text
    assert "4 discovered · 3 processed · 2 model calls" in listing.text
    assert 'href="/operations/runs/00000000-0000-0000-0000-000000000002"' in listing.text
    assert "Job</strong>" in listing.text
    assert ">Retrying</span>" in listing.text
    assert "provider_timeout: OpenRouter did not respond" in listing.text
    assert 'href="/operations/work/00000000-0000-0000-0000-000000000009"' in listing.text
    assert "Open job →" in listing.text
    assert 'class="row-head"' in listing.text
    assert 'href="/operations/runs?cursor=next-cursor-token"' in listing.text
    assert "Next page →" in listing.text


def test_the_activity_page_renders_the_filter_form() -> None:
    activity, _ = _activity_service()
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs")

    assert listing.status_code == 200
    assert 'name="status" value="failed"' in listing.text
    assert 'name="kind"' in listing.text
    assert '<option value="orchestration">Scheduler tick</option>' in listing.text
    assert '<option value="evaluation">Evaluation run</option>' in listing.text
    assert "<span>Needs attention</span>" in listing.text
    assert 'name="from"' in listing.text
    assert "Apply filters" in listing.text


def test_the_activity_page_counts_hidden_entries() -> None:
    activity, _ = _activity_service(hidden_no_op_count=2)
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs")

    assert "2 entries that did nothing hidden." in listing.text
    assert "All matching activity is hidden." in listing.text
