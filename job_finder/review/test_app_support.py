from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import re
from typing import Literal, Never, cast
from uuid import UUID

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from job_finder.config import ReviewAppSettings
from job_finder.evaluation.models import PromptReleaseId, RelevanceReleaseId
from job_finder.onboarding_test_search import (
    OnboardingTestSearchAccepted,
    OnboardingTestSearchLimits,
    OnboardingTestSearchRequest,
)
from job_finder.review.onboarding import (
    OnboardingSearchProgress,
    OnboardingSearchService,
)
from job_finder.operations.spend import (
    AnalyticsService,
    DayModelSpend,
    DaySpend,
    ModelSpend,
    SpendAnalytics,
)
from job_finder.web.app import create_review_app
from job_finder.operations.control_plane import (
    CONTROL_DEFINITIONS,
    ControlPlaneService,
    ControlPlaneSnapshot,
    RunNowCommand,
    RunNowResult,
    ScheduleChangeCommand,
    ScheduleChangeResult,
    ScheduleStatus,
    ScheduleView,
)
from job_finder.review.feedback import (
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)
from job_finder.review.queue import (
    ReviewDecision,
    ReviewItem,
    ReviewJob,
    ReviewLane,
    ReviewQueue,
)
from job_finder.pipeline.work_dismissals import (
    WorkDismissalApplied,
    WorkDismissalCommand,
    WorkDismissalReceipt,
)
from job_finder.pipeline.work_recoveries import (
    RecoveryOutcome,
    WorkRecoveryApplied,
    WorkRecoveryCommand,
    WorkRecoveryReceipt,
)
from job_finder.pipeline.reevaluations import (
    JobReevaluationAccepted,
    JobReevaluationCommand,
    JobReevaluationReceipt,
)
from job_finder.operations._common import PipelineRunStatus, WorkItemState
from job_finder.operations.activity import (
    ActivityEntry,
    ActivityPage,
    ActivityQuery,
    ActivityRun,
    ActivityService,
    ActivityWork,
)
from job_finder.operations.health import (
    OperationsHealth,
    OperationsSnapshot,
    QueueCounts,
    SpendSummary,
)
from job_finder.operations.run_history import RunListItem, RunsService
from job_finder.operations.service import OperationsService
from job_finder.operations.work_history import (
    ModelCallDetail,
    WorkAttemptSummary,
    JobVerdict,
    WorkItemDetail,
)
from job_finder.review.owner_access import (
    OnboardingStage,
    OwnerAccessService,
    OwnerAccessState,
    OwnerBootstrapConflict,
)
from job_finder.search_configuration import SearchConfigurationRevisionId

TODAY = date(2026, 9, 10)

YESTERDAY = date(2026, 9, 9)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

SETTINGS = ReviewAppSettings(
    bootstrap_token=SecretStr("bootstrap-token-with-at-least-32-characters"),
    session_secret="s" * 32,
    cookie_secure=False,
)

OWNER_PASSWORD = "correct horse battery staple"

BOOTSTRAP_TOKEN = "bootstrap-token-with-at-least-32-characters"

OWNER_STATE = OwnerAccessState(stage=OnboardingStage.COMPLETE, has_password=True)

OWNER_ACCESS = OwnerAccessService(
    load_state=lambda: OWNER_STATE,
    authenticate=lambda password: password == OWNER_PASSWORD,
    bootstrap=lambda _password: OwnerBootstrapConflict(OWNER_STATE),
)


def helper_default_submit_review(_review: ReviewSubmission) -> ReviewSubmitResult:
    return ReviewSaved(review_event_id=UUID(int=9))


Submitter = Callable[[ReviewSubmission], ReviewSubmitResult]

CONTROL_DESCRIPTION_MATCHES = {
    "job_finder": "registers what it finds in the work queue",
    "job_work_queue": "claims due jobs",
    "onboarding_test_search": "bounded test search requested during setup",
    "review_sample": "a sample of yesterday's rejected jobs",
    "langfuse_projection": "Ships telemetry to Langfuse",
}


def helper_unexpected_control_call(*_args: object) -> Never:
    raise AssertionError("control operation was not expected")


def helper_test_search_request(
    state: Literal["pending", "leased", "completed", "failed"],
) -> OnboardingTestSearchRequest:
    return OnboardingTestSearchRequest(
        idempotency_key="owner-setup:test",
        run_id=UUID(int=41),
        state=state,
        configuration_revision_id=SearchConfigurationRevisionId("a" * 64),
        prompt_release_id=PromptReleaseId("b" * 64),
        relevance_release_id=RelevanceReleaseId("c" * 64),
        release_generation=1,
        budget_policy_version=1,
        budget_reservation_key="onboarding-test-search:owner-setup:test",
        limits=OnboardingTestSearchLimits(
            max_queries=3,
            max_urls=10,
            max_jobs=2,
            max_work_attempts=3,
            max_provider_attempts=20,
            run_allowance_usd=Decimal("2"),
        ),
        attempt_count=1,
        provider_attempt_count=2,
        owner_token=None,
        lease_expires_at=None,
        error_code="provider_outage" if state == "failed" else None,
        error_reason="The provider did not respond" if state == "failed" else None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW if state in {"completed", "failed"} else None,
    )


def helper_test_search_client() -> (
    tuple[
        TestClient,
        list[OwnerAccessState],
        list[OnboardingSearchProgress],
        list[tuple[str, datetime]],
    ]
):
    owner_state = [OwnerAccessState(stage=OnboardingStage.TEST_SEARCH, has_password=True)]
    progress = [OnboardingSearchProgress(request=None)]
    launches: list[tuple[str, datetime]] = []

    def launch(actor: str, timestamp: datetime) -> OnboardingTestSearchAccepted:
        launches.append((actor, timestamp))
        request = helper_test_search_request("pending")
        progress[0] = OnboardingSearchProgress(request=request)
        return OnboardingTestSearchAccepted(replayed=False, request=request)

    owner = OwnerAccessService(
        load_state=lambda: owner_state[0],
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    client = TestClient(
        create_review_app(
            lambda: helper_queue(),
            SETTINGS,
            submit_review=helper_default_submit_review,
            owner_access_service=owner,
            test_search_service=OnboardingSearchService(inspect=lambda: progress[0], launch=launch),
            now=lambda: NOW,
        )
    )
    helper_authenticate(client)
    return client, owner_state, progress, launches


def helper_saved(_review: ReviewSubmission) -> ReviewSaved:
    return ReviewSaved(review_event_id=UUID(int=9))


def helper_client(
    queue: ReviewQueue,
    submit: Submitter = helper_saved,
    *,
    operations: OperationsService | None = None,
    runs: RunsService | None = None,
    activity: ActivityService | None = None,
    analytics: AnalyticsService | None = None,
    controls: ControlPlaneService | None = None,
) -> TestClient:
    client = TestClient(
        create_review_app(
            lambda: queue,
            SETTINGS,
            submit_review=submit,
            owner_access_service=OWNER_ACCESS,
            operations_service=operations,
            runs_service=runs,
            activity_service=activity,
            analytics_service=analytics,
            control_service=controls,
            now=lambda: NOW,
        )
    )
    helper_authenticate(client)
    return client


def helper_draining_client(remaining: list[ReviewItem], submit: Submitter) -> TestClient:
    client = TestClient(
        create_review_app(
            lambda: ReviewQueue(items=tuple(remaining)),
            SETTINGS,
            submit_review=submit,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )
    helper_authenticate(client)
    return client


def helper_authenticate(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": "/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def helper_queue(*items: ReviewItem) -> ReviewQueue:
    return ReviewQueue(items=items)


def helper_item(review_day: date, lane: ReviewLane, *, value: int = 1) -> ReviewItem:
    return ReviewItem(
        review_day=review_day,
        id=UUID(int=value),
        evaluation_id=f"{value:064x}",
        snapshot_id=f"{value + 10:064x}",
        lane=lane,
        position=value - 1,
        outcome="qualified" if lane == "qualified" else "rejected",
        matched_profile="applied-ai-product-engineer" if lane == "qualified" else None,
        evaluation_reason="Strong product delivery fit.",
        job=ReviewJob(
            title=f"Applied AI Engineer {value}",
            company="Acme",
            url=f"https://example.com/jobs/{value}",
            source="other",
            description="## Overview\nBuild useful tools.",
            location="Remote",
            keywords=("python",),
            date_posted=date(2026, 9, 9),
        ),
    )


def helper_decided(
    item: ReviewItem, decision: ReviewDecision, note: str | None = None
) -> ReviewItem:
    return item.model_copy(update={"reviewed": True, "decision": decision, "note": note})


def helper_form(item: ReviewItem, client: TestClient) -> dict[str, str]:
    return {
        "csrf_token": helper_csrf(client),
        "evaluation_id": item.evaluation_id,
        "snapshot_id": item.snapshot_id,
        "decision": "reject",
    }


def helper_csrf(client: TestClient) -> str:
    response = client.get("/review")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def helper_shell_link(href: str, label: str, *, current: bool = False) -> str:
    pattern = rf'<a(?=[^>]*href="{re.escape(href)}")'
    if current:
        pattern += r'(?=[^>]*aria-current="page")'
    pattern += rf'(?=[^>]*class="shell-link")[^>]*>{re.escape(label)}</a>'
    return pattern


def helper_control_service(
    *,
    run_now: Callable[[RunNowCommand], RunNowResult] | None = None,
    change_schedule: Callable[[ScheduleChangeCommand], ScheduleChangeResult] | None = None,
) -> ControlPlaneService:
    snapshot = ControlPlaneSnapshot(
        schedules=tuple(
            ScheduleView(
                definition=definition,
                status=ScheduleStatus.RUNNING,
                next_tick=datetime(2026, 9, 21, 13, tzinfo=UTC),
            )
            for definition in CONTROL_DEFINITIONS
        )
    )

    def unexpected(*_args: object) -> Never:
        raise AssertionError("control operation was not expected")

    return ControlPlaneService(
        load=lambda: snapshot,
        run_now=cast(Callable[..., Never], unexpected) if run_now is None else run_now,
        change_schedule=cast(Callable[..., Never], unexpected)
        if change_schedule is None
        else change_schedule,
    )


def helper_operations_snapshot() -> OperationsSnapshot:
    return OperationsSnapshot(
        health=OperationsHealth.UNKNOWN,
        queues=QueueCounts(),
        spend=SpendSummary(known_usd=Decimal(0), unknown_attempts=0),
        recent_runs=(),
        failures=(),
    )


def helper_run_item(
    *,
    value: int = 1,
    kind: str = "orchestration",
    status: PipelineRunStatus = "completed",
    idle: bool = False,
) -> RunListItem:
    return RunListItem(
        id=UUID(int=value),
        kind=kind,
        status=status,
        started_at=NOW,
        completed_at=NOW if status != "running" else None,
        discoveries=0 if idle else 4,
        processing_attempts=0 if idle else 5,
        processed_jobs=0 if idle else 3,
        model_calls=0 if idle else 2,
        known_cost_usd=Decimal("0.5010") if not idle else Decimal(0),
        error_summary=None,
    )


def helper_activity_run_entry(
    *, value: int = 1, kind: str = "orchestration", idle: bool = False
) -> ActivityEntry:
    item = helper_run_item(value=value, kind=kind, idle=idle)
    return ActivityEntry(
        occurred_at=item.started_at,
        ref=str(item.id),
        item=ActivityRun(
            id=item.id,
            kind=item.kind,
            status=item.status,
            started_at=item.started_at,
            completed_at=item.completed_at,
            discoveries=item.discoveries,
            processing_attempts=item.processing_attempts,
            processed_jobs=item.processed_jobs,
            model_calls=item.model_calls,
            known_cost_usd=item.known_cost_usd,
            error_summary=item.error_summary,
        ),
    )


def helper_activity_work_entry(
    *,
    value: int = 9,
    state: str = "failed",
    dismissed: bool = False,
) -> ActivityEntry:
    return ActivityEntry(
        occurred_at=NOW,
        ref=str(UUID(int=value)),
        item=ActivityWork(
            job_id=UUID(int=value),
            state=cast(WorkItemState, state),
            attempt_count=2,
            occurred_at=NOW,
            retry_at=NOW if state == "failed" else None,
            failure_summary="provider_timeout: OpenRouter did not respond",
            dismissed=dismissed,
        ),
    )


def helper_activity_service(
    *entries: ActivityEntry, cursor: str | None = None, hidden_no_op_count: int = 0
) -> tuple[ActivityService, list[ActivityQuery]]:
    captured: list[ActivityQuery] = []

    def list_page(query: ActivityQuery) -> ActivityPage:
        captured.append(query)
        return ActivityPage(
            entries=tuple(entries),
            next_cursor=cursor,
            hidden_no_op_count=hidden_no_op_count,
        )

    return ActivityService(list=list_page), captured


def helper_work_item_detail(
    *,
    state: WorkItemState = "terminal_error",
    retry_at: datetime | None = None,
    dismissed: bool = False,
    dismissed_at: datetime | None = None,
    dismissed_by: str | None = None,
    verdict: JobVerdict | None = None,
    calls: tuple[ModelCallDetail, ...] = (),
) -> WorkItemDetail:
    return WorkItemDetail(
        job_id=UUID(int=31),
        state=state,
        attempt_count=3,
        created_at=NOW - timedelta(hours=2),
        completed_at=NOW - timedelta(hours=1, minutes=59),
        last_failed_at=NOW - timedelta(minutes=30),
        retry_at=retry_at,
        failure_summary="provider_timeout: OpenRouter did not respond",
        discovery_run_id=UUID(int=7),
        dismissed=dismissed,
        dismissed_at=dismissed_at,
        dismissed_by=dismissed_by,
        verdict=verdict,
        attempts=(
            WorkAttemptSummary(
                operation_key="evaluation",
                attempt_number=2,
                status="failed",
                started_at=NOW - timedelta(minutes=30),
                completed_at=NOW - timedelta(minutes=29),
                run_id=UUID(int=7),
                model_calls=2,
                known_cost_usd=Decimal("0.25"),
                error_summary="provider_timeout: OpenRouter did not respond",
                calls=calls,
            ),
        ),
    )


def helper_spend_analytics() -> SpendAnalytics:
    return SpendAnalytics(
        known_usd=Decimal("1.2345"),
        calls=6,
        accepted=4,
        errors=2,
        input_tokens=900,
        output_tokens=300,
        max_latency_ms=5200,
        days=(
            DaySpend(
                day=NOW.date(),
                calls=4,
                accepted=3,
                errors=1,
                known_cost_usd=Decimal("1.0000"),
                by_model=(
                    DayModelSpend(
                        model="z-ai/glm-4.6",
                        known_cost_usd=Decimal("0.9000"),
                        p90_latency_ms=3000,
                    ),
                    DayModelSpend(
                        model="openai/gpt-5-mini",
                        known_cost_usd=Decimal("0.1000"),
                        p90_latency_ms=1200,
                    ),
                ),
            ),
            DaySpend(
                day=NOW.date() - timedelta(days=1),
                calls=2,
                accepted=1,
                errors=1,
                known_cost_usd=Decimal("0.2345"),
                by_model=(
                    DayModelSpend(
                        model="z-ai/glm-4.6",
                        known_cost_usd=Decimal("0.2345"),
                        p90_latency_ms=4100,
                    ),
                ),
            ),
        ),
        models=(
            ModelSpend(
                model="z-ai/glm-4.6",
                calls=4,
                accepted=3,
                errors=1,
                input_tokens=700,
                output_tokens=200,
                known_cost_usd=Decimal("1.1000"),
                max_latency_ms=5200,
            ),
            ModelSpend(
                model="openai/gpt-5-mini",
                calls=2,
                accepted=1,
                errors=1,
                input_tokens=200,
                output_tokens=100,
                known_cost_usd=Decimal("0.1345"),
                max_latency_ms=2100,
            ),
        ),
    )


def helper_applied_dismissal(command: WorkDismissalCommand) -> WorkDismissalApplied:
    return WorkDismissalApplied(
        receipt=WorkDismissalReceipt(
            idempotency_key=command.idempotency_key,
            job_id=command.job_id,
            action=command.action,
            expected_attempt_count=command.expected_attempt_count,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome="applied",
            prior_state="terminal_error",
            prior_attempt_count=command.expected_attempt_count,
            resulting_attempt_count=command.expected_attempt_count,
        ),
        replayed=False,
    )


def helper_applied_recovery(command: WorkRecoveryCommand) -> WorkRecoveryApplied:
    return WorkRecoveryApplied(
        receipt=helper_recovery_receipt(
            command, outcome="applied", prior_state=command.expected_state
        ),
        replayed=False,
    )


def helper_accepted_reevaluation(command: JobReevaluationCommand) -> JobReevaluationAccepted:
    return JobReevaluationAccepted(
        receipt=JobReevaluationReceipt(
            idempotency_key=command.idempotency_key,
            expected_decision_id=command.expected_decision_id,
            expected_snapshot_id=command.expected_snapshot_id,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome="accepted",
        ),
        replayed=False,
    )


def helper_recovery_receipt(
    command: WorkRecoveryCommand,
    *,
    outcome: RecoveryOutcome,
    prior_state: WorkItemState,
) -> WorkRecoveryReceipt:
    return WorkRecoveryReceipt(
        idempotency_key=command.idempotency_key,
        job_id=command.job_id,
        action=command.action,
        expected_state=command.expected_state,
        expected_attempt_count=command.expected_attempt_count,
        actor=command.actor,
        requested_at=command.requested_at,
        outcome=outcome,
        prior_state=prior_state,
        prior_attempt_count=2,
        prior_retry_at=NOW if prior_state == "failed" else None,
        prior_failed_at=NOW,
        prior_error={"code": "failure", "reason": "failed"},
        resulting_state=prior_state,
        resulting_attempt_count=2,
        resulting_retry_at=NOW if prior_state == "failed" else None,
    )
