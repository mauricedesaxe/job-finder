from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fasthtml.common import FastHTML
from starlette.routing import Route
from starlette.testclient import TestClient

from job_finder.access_policy import (
    Capability,
    Preset,
    RouteAccess,
    RoutePolicy,
    has_capability,
    landing_path,
    preset_grants,
    route_policy,
)
from job_finder.database import ConnectionFactory
from job_finder.review.test_app_support import (
    helper_account_service,
    NOW,
    OWNER_ACCESS,
    SETTINGS,
    helper_default_submit_review,
    helper_queue,
)
from job_finder.web.app import create_review_app
from job_finder.web.route_policy import compile_route_policies


def _policy(method: str, path: str) -> RoutePolicy:
    policy = route_policy(method, path)
    assert policy is not None
    return policy


@pytest.mark.parametrize("split", [False, True])
def test_review_routes_all_have_policy(split: bool) -> None:
    settings = SETTINGS.model_copy(
        update={"split_execution_artifact_path": Path("/tmp/split.json") if split else None}
    )
    app = create_review_app(
        helper_queue,
        settings,
        submit_review=helper_default_submit_review,
        account_service=helper_account_service(),
        owner_access_service=OWNER_ACCESS,
        split_configuration_connect=cast(ConnectionFactory, lambda: None) if split else None,
        now=lambda: NOW,
    )
    bound = compile_route_policies(app, review_routes=True, split_routes=split)
    routes = [route for route in app.routes if isinstance(route, Route)]
    assert len(bound) == sum(len(route.methods or ()) for route in routes)
    assert route_policy("HEAD", "/static/{name}") == route_policy("GET", "/static/{name}")


def test_missing_route_policy_fails_app_startup() -> None:
    app = FastHTML(default_hdrs=False)

    @app.route("/surprise", methods=["POST"])
    def surprise() -> str:
        return "unexpected"

    with pytest.raises(ValueError, match="no access policy"):
        compile_route_policies(app, review_routes=False)


def test_route_policy_classifies_login_onboarding_and_sensitive_actions() -> None:
    assert _policy("GET", "/healthz").access is RouteAccess.PUBLIC
    assert _policy("POST", "/login").access is RouteAccess.LOGIN
    assert _policy("POST", "/logout").access is RouteAccess.AUTHENTICATED
    assert _policy("GET", "/static/{name}").access is RouteAccess.AUTHENTICATED
    assert _policy("POST", "/setup/providers").access is RouteAccess.ONBOARDING
    assert _policy("GET", "/setup/providers").post_setup_capability is Capability.ADMIN_VIEW
    assert _policy("POST", "/setup/providers").post_setup_capability is Capability.ADMIN_CREDENTIALS
    assert _policy("GET", "/setup/budget").post_setup_capability is Capability.ADMIN_VIEW
    assert _policy("POST", "/setup/budget").post_setup_capability is Capability.ADMIN_BUDGET
    assert _policy("POST", "/setup/test-search").post_setup_capability is None
    assert _policy("POST", "/setup/providers/continue").post_setup_capability is None
    assert _policy("POST", "/configuration/continue").access is RouteAccess.ONBOARDING
    assert _policy("POST", "/review/{review_item_id}").capability is Capability.REVIEW_SUBMIT
    assert _policy("POST", "/operations/reevaluation").capability is Capability.ACTIVITY_REEVALUATE
    assert (
        _policy("POST", "/configuration/qualification-promotion/preview").capability
        is Capability.SEARCH_VIEW
    )
    assert route_policy("POST", "/not-registered") is None


def test_logout_requires_a_session_in_the_current_guard() -> None:
    app = create_review_app(
        helper_queue,
        SETTINGS,
        submit_review=helper_default_submit_review,
        account_service=helper_account_service(),
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )

    response = TestClient(app).post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Flogout"


def test_preset_grants_are_independent_snapshots() -> None:
    assert preset_grants(Preset.ADMIN) == frozenset(Capability)
    assert Capability.ADMIN_VIEW in preset_grants(Preset.ADMIN)
    assert Capability.SEARCH_ACTIVATE in preset_grants(Preset.SEARCH_MANAGER)
    assert Capability.REVIEW_SUBMIT in preset_grants(Preset.REVIEWER)
    assert Capability.CONTROL_RUN in preset_grants(Preset.OPERATOR)
    assert Capability.ADMIN_MEMBERS not in preset_grants(Preset.OPERATOR)
    assert landing_path(preset_grants(Preset.SEARCH_MANAGER)) == "/configuration"
    assert landing_path(preset_grants(Preset.OPERATOR)) == "/operations/runs"
    assert landing_path(frozenset({Capability.ANALYTICS_VIEW})) == "/operations/analytics"
    assert landing_path(frozenset({Capability.CONTROL_VIEW})) == "/operations/control"
    assert landing_path(frozenset({Capability.ADMIN_VIEW})) == "/members"
    assert landing_path(frozenset()) == "/no-access"


def test_custom_action_grants_require_the_area_view_grant() -> None:
    for action, view in (
        (Capability.REVIEW_SUBMIT, Capability.REVIEW_VIEW),
        (Capability.SEARCH_DRAFT, Capability.SEARCH_VIEW),
        (Capability.SEARCH_PUBLISH, Capability.SEARCH_VIEW),
        (Capability.SEARCH_ACTIVATE, Capability.SEARCH_VIEW),
        (Capability.ACTIVITY_RECOVER, Capability.ACTIVITY_VIEW),
        (Capability.ACTIVITY_DISMISS, Capability.ACTIVITY_VIEW),
        (Capability.ACTIVITY_REEVALUATE, Capability.ACTIVITY_VIEW),
        (Capability.CONTROL_RUN, Capability.CONTROL_VIEW),
        (Capability.CONTROL_SCHEDULE, Capability.CONTROL_VIEW),
    ):
        assert not has_capability(frozenset({action}), action)
        assert has_capability(frozenset({action, view}), action)
        assert not has_capability(frozenset({view}), action)
    assert not has_capability(frozenset({Capability.ADMIN_MEMBERS}), Capability.ADMIN_MEMBERS)
    for action in (
        Capability.ADMIN_MEMBERS,
        Capability.ADMIN_CREDENTIALS,
        Capability.ADMIN_BUDGET,
    ):
        assert not has_capability(frozenset({action}), action)
        assert has_capability(frozenset({Capability.ADMIN_VIEW, action}), action)
    assert not has_capability(frozenset(), Capability.ADMIN_VIEW)
