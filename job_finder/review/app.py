# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fasthtml.common import (
    FastHTML,
    Request,
)
from starlette.responses import Response

from job_finder.config import ReviewAppSettings
from job_finder.execution_budget import (
    BudgetSetupService,
)
from job_finder.operations.activity import (
    ActivityService,
)
from job_finder.operations.control_plane import (
    ControlPlaneService,
    unavailable_control_plane_service,
)
from job_finder.operations.run_history import RunsService
from job_finder.operations.service import OperationsService, unknown_operations_service
from job_finder.operations.spend import (
    AnalyticsService,
)
from job_finder.operations.web import register_operations_routes
from job_finder.provider_credentials import (
    ProviderSetupService,
)
from job_finder.review.access_web import register_access_routes, require_owner as access_guard
from job_finder.review.configuration import register_configuration_routes
from job_finder.review.configuration_editor import ConfigurationEditorService
from job_finder.review.feedback import ReviewFeedbackService
from job_finder.review.onboarding import (
    OnboardingProgressService,
    OnboardingSearchService,
)
from job_finder.review.owner_access import (
    OwnerAccessService,
)
from job_finder.review.queue import ReviewQueueService
from job_finder.review.workbench import ReviewWorkbench
from job_finder.web.app import ReadinessProbe, create_web_app

DateTimeClock = Callable[[], datetime]


def create_review_app(
    queue_service: ReviewQueueService,
    configuration_service: ConfigurationEditorService,
    settings: ReviewAppSettings,
    *,
    feedback_service: ReviewFeedbackService,
    owner_access_service: OwnerAccessService,
    provider_setup_service: ProviderSetupService | None = None,
    onboarding_progress_service: OnboardingProgressService | None = None,
    test_search_service: OnboardingSearchService | None = None,
    budget_setup_service: BudgetSetupService | None = None,
    readiness: ReadinessProbe = lambda: None,
    operations_service: OperationsService | None = None,
    runs_service: RunsService | None = None,
    activity_service: ActivityService | None = None,
    analytics_service: AnalyticsService | None = None,
    control_service: ControlPlaneService | None = None,
    actor: str = "owner",
    now: DateTimeClock = lambda: datetime.now(UTC),
) -> FastHTML:
    operations = operations_service or unknown_operations_service()
    runs = runs_service or RunsService()
    activity = activity_service or ActivityService()
    analytics = analytics_service or AnalyticsService()
    controls = control_service or unavailable_control_plane_service()
    dagster_configured = control_service is not None
    workbench = ReviewWorkbench(
        queue_service=queue_service,
        feedback_service=feedback_service,
        actor=actor,
        now=now,
    )

    def require_owner(request: Request) -> Response | None:
        return access_guard(request, owner_access_service, budget_setup_service)

    app = create_web_app(settings, guard=require_owner, readiness=readiness)
    register_access_routes(
        app,
        settings=settings,
        owner_access_service=owner_access_service,
        provider_setup_service=provider_setup_service,
        budget_setup_service=budget_setup_service,
        test_search_service=test_search_service,
        actor=actor,
        now=now,
    )

    workbench.register_queue_routes(app)

    register_operations_routes(
        app,
        operations=operations,
        runs=runs,
        activity=activity,
        analytics=analytics,
        controls=controls,
        dagster_configured=dagster_configured,
        actor=actor,
        now=now,
    )

    register_configuration_routes(
        app,
        configuration_service=configuration_service,
        owner_access_service=owner_access_service,
        onboarding_progress_service=onboarding_progress_service,
        actor=actor,
        now=now,
    )

    workbench.register_item_routes(app)

    return app
