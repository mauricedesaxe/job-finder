# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false
from __future__ import annotations

import logging
from collections.abc import Callable

import psycopg
from fasthtml.common import Beforeware, FastHTML
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from job_finder.config import ReviewAppSettings
from datetime import UTC, datetime

from job_finder.database import ConnectionFactory
from job_finder.execution_budget import BudgetSetupService
from job_finder.evaluation.relevance_releases import RelevanceReleaseError
from job_finder.operations.activity import ActivityService
from job_finder.operations.control_plane import (
    ControlPlaneService,
    unavailable_control_plane_service,
)
from job_finder.operations.run_history import RunsService
from job_finder.operations.service import OperationsService, unknown_operations_service
from job_finder.operations.spend import AnalyticsService
from job_finder.operations.web import register_operations_routes
from job_finder.provider_credentials import ProviderSetupService
from job_finder.review.access_guard import require_account as access_guard
from job_finder.review.access_web import register_access_routes
from job_finder.review.accounts import AccountService
from job_finder.review.members_web import register_member_routes
from job_finder.review.split_configuration import register_split_configuration_routes
from job_finder.review.qualification_targets import register_qualification_target_routes
from job_finder.review.qualification_promotions_web import register_qualification_promotion_routes
from job_finder.review.feedback import ReviewSubmitter
from job_finder.review.onboarding import OnboardingSearchService
from job_finder.review.owner_access import OwnerAccessService
from job_finder.review.queue import ReviewQueueLoader
from job_finder.review.workbench import ReviewWorkbench

from job_finder.web.assets import static_asset_path
from job_finder.web.route_policy import compile_route_policies
from job_finder.web.security import SecurityHeadersMiddleware

ReadinessProbe = Callable[[], None]
RequestGuard = Callable[[Request], Response | None]

_SESSION_COOKIE = "job_finder_review_session"
_SESSION_MAX_AGE = 14 * 24 * 60 * 60
_logger = logging.getLogger(__name__)


def create_web_app(
    settings: ReviewAppSettings,
    *,
    guard: RequestGuard,
    readiness: ReadinessProbe,
) -> FastHTML:
    app = FastHTML(
        before=Beforeware(
            guard,
            skip=[r"/healthz", r"/readyz", r"/favicon.ico"],
        ),
        default_hdrs=False,
        htmx=False,
        surreal=False,
        secret_key=settings.session_secret,
        session_cookie=_SESSION_COOKIE,
        max_age=_SESSION_MAX_AGE,
        same_site="lax",
        sess_https_only=settings.cookie_secure,
    )

    @app.route("/healthz", methods=["GET"], name="create_review_app_healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.route("/readyz", methods=["GET"], name="create_review_app_readyz")
    def readyz() -> PlainTextResponse:
        try:
            readiness()
        except psycopg.Error as error:
            _logger.error("Readiness database check failed: %s", type(error).__name__)
            return PlainTextResponse("database unavailable", status_code=503)
        except RelevanceReleaseError:
            _logger.exception("Readiness failed because the active relevance release is invalid")
            return PlainTextResponse("active release incompatible", status_code=503)
        except RuntimeError as error:
            _logger.error("Readiness state check failed: %s", type(error).__name__)
            return PlainTextResponse("application state unavailable", status_code=503)
        return PlainTextResponse("ready")

    @app.route("/favicon.ico", methods=["GET"], name="create_review_app_favicon")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.route("/static/{name}", methods=["GET"], name="create_review_app_static_asset")
    def static_asset(name: str) -> Response:
        path = static_asset_path(name)
        if path is None or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    app.add_middleware(SecurityHeadersMiddleware)
    app.state.route_policies = compile_route_policies(app, review_routes=False)
    return app


_DateTimeClock = Callable[[], datetime]


def create_review_app(
    load_review_queue: ReviewQueueLoader,
    settings: ReviewAppSettings,
    *,
    submit_review: ReviewSubmitter,
    account_service: AccountService,
    owner_access_service: OwnerAccessService,
    provider_setup_service: ProviderSetupService | None = None,
    test_search_service: OnboardingSearchService | None = None,
    budget_setup_service: BudgetSetupService | None = None,
    readiness: ReadinessProbe = lambda: None,
    operations_service: OperationsService | None = None,
    runs_service: RunsService | None = None,
    activity_service: ActivityService | None = None,
    analytics_service: AnalyticsService | None = None,
    control_service: ControlPlaneService | None = None,
    now: _DateTimeClock = lambda: datetime.now(UTC),
    split_configuration_connect: ConnectionFactory | None = None,
) -> FastHTML:
    operations = operations_service or unknown_operations_service()
    runs = runs_service or RunsService()
    activity = activity_service or ActivityService()
    analytics = analytics_service or AnalyticsService()
    controls = control_service or unavailable_control_plane_service()
    dagster_configured = control_service is not None
    workbench = ReviewWorkbench(
        load_queue=load_review_queue,
        submit_review=submit_review,
        now=now,
    )

    def require_account(request: Request) -> Response | None:
        policy = app.state.route_policies.get((request.scope.get("endpoint"), request.method))
        if policy is None:
            return Response(status_code=403)
        return access_guard(
            request,
            owner_access_service,
            account_service,
            budget_setup_service,
            policy,
        )

    app = create_web_app(settings, guard=require_account, readiness=readiness)
    register_access_routes(
        app,
        settings=settings,
        account_service=account_service,
        owner_access_service=owner_access_service,
        provider_setup_service=provider_setup_service,
        budget_setup_service=budget_setup_service,
        test_search_service=test_search_service,
        now=now,
    )
    register_member_routes(app, accounts=account_service)

    workbench.register_queue_routes(app)

    register_operations_routes(
        app,
        operations=operations,
        runs=runs,
        activity=activity,
        analytics=analytics,
        controls=controls,
        dagster_configured=dagster_configured,
        now=now,
    )

    if settings.split_execution_artifact_path is not None:
        if split_configuration_connect is None:
            raise ValueError("Split search setup requires a database connection")
        register_split_configuration_routes(app, connect=split_configuration_connect, now=now)
        register_qualification_target_routes(
            app,
            connect=split_configuration_connect,
            artifact_path=settings.split_execution_artifact_path,
            now=now,
        )
        register_qualification_promotion_routes(
            app,
            connect=split_configuration_connect,
            artifact_path=settings.split_execution_artifact_path,
            now=now,
        )
    workbench.register_item_routes(app)

    app.state.route_policies = compile_route_policies(
        app,
        review_routes=True,
        split_routes=settings.split_execution_artifact_path is not None,
    )

    return app
