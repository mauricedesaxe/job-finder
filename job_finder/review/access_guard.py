"""Authenticate accounts and enforce route capabilities at request entry."""

from __future__ import annotations

import logging
from urllib.parse import quote

import psycopg
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from job_finder.access_policy import RouteAccess, RoutePolicy, has_capability
from job_finder.evaluation.relevance_releases import RelevanceReleaseError
from job_finder.execution_budget import BudgetSetupService
from job_finder.review.accounts import AccountService
from job_finder.review.owner_access import OnboardingStage, OwnerAccessService
from job_finder.web.shell import state_response

logger = logging.getLogger(__name__)


def require_account(
    request: Request,
    owner_access: OwnerAccessService,
    accounts: AccountService,
    budget_setup: BudgetSetupService | None,
    route_policy: RoutePolicy,
) -> Response | None:
    if ".." in request.url.path.split("/"):
        return Response(status_code=404)
    if route_policy.access is RouteAccess.PUBLIC:
        return None
    try:
        state = owner_access.load_state()
        has_accounts = accounts.has_accounts()
    except (psycopg.Error, RuntimeError) as error:
        logger.error("Account access unavailable: %s", type(error).__name__)
        return state_response(
            "Account access is unavailable",
            "Installation state could not be loaded. Check the server logs.",
            status_code=503,
        )
    if state.stage is OnboardingStage.LEGACY_OWNER_IMPORT and not state.has_password:
        return state_response(
            "Legacy owner import is required",
            "Restore JOB_FINDER_REVIEW_PASSWORD for one startup to import the existing owner securely.",
            status_code=503,
        )
    if not has_accounts:
        if request.url.path == "/setup":
            return None
        return RedirectResponse("/setup", status_code=303)

    token = request.session.get("account_session")
    try:
        principal = accounts.load_principal(token) if isinstance(token, str) else None
    except psycopg.Error as error:
        logger.error("Account session unavailable: %s", type(error).__name__)
        return state_response(
            "Account access is unavailable",
            "Your session could not be checked. Check the server logs.",
            status_code=503,
        )
    request.state.principal = principal
    if route_policy.access is RouteAccess.LOGIN:
        return None
    if principal is None:
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(next_url, safe='')}", status_code=303)
    if request.url.path == "/setup":
        return RedirectResponse("/", status_code=303)

    onboarding_path = {
        OnboardingStage.PROVIDERS: "/setup/providers",
        OnboardingStage.PREFERENCES: "/configuration",
        OnboardingStage.BUDGET: "/setup/budget",
        OnboardingStage.TEST_SEARCH: "/setup/test-search",
    }.get(state.stage)
    during_onboarding = onboarding_path is not None
    if onboarding_path is not None:
        allowed_prefix = (
            "/configuration" if state.stage is OnboardingStage.PREFERENCES else onboarding_path
        )
        if request.url.path != "/logout" and not request.url.path.startswith(allowed_prefix):
            return RedirectResponse(onboarding_path, status_code=303)

    if (
        not during_onboarding
        and budget_setup is not None
        and request.url.path not in ("/logout", "/setup/budget")
    ):
        try:
            budget_policy = budget_setup.inspect(25).policy
        except RelevanceReleaseError:
            logger.exception("Budget inspection failed because the relevance release is invalid")
            return state_response(
                "Budget setup is unavailable",
                "The active relevance release could not be validated. Activate a release compatible with this deployment, then reload.",
                status_code=503,
            )
        except psycopg.Error as error:
            logger.error("Budget database inspection failed: %s", type(error).__name__)
            return state_response(
                "Budget setup is unavailable",
                "Budget database state could not be read. Check the server logs and retry.",
                status_code=503,
            )
        except RuntimeError as error:
            logger.error("Budget inspection failed: %s", type(error).__name__)
            return state_response(
                "Budget setup is unavailable",
                "Budget state could not be loaded. Check the server logs.",
                status_code=503,
            )
        if budget_policy is None:
            return RedirectResponse("/setup/budget", status_code=303)

    if route_policy.access is RouteAccess.AUTHENTICATED:
        return None
    if route_policy.access is RouteAccess.ONBOARDING:
        if during_onboarding:
            return None
        if route_policy.post_setup_capability is None:
            return Response(status_code=404)
        required = route_policy.post_setup_capability
    else:
        required = route_policy.capability
    if required is None or not has_capability(principal.capabilities, required):
        return state_response(
            "Access denied",
            "Your account cannot use this action.",
            status_code=403,
            eyebrow="Account access",
        )
    return None
