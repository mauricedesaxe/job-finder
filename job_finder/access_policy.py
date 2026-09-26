"""Capability grants and route access policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Capability(StrEnum):
    REVIEW_VIEW = "review.view"
    REVIEW_SUBMIT = "review.submit"
    SEARCH_VIEW = "search.view"
    SEARCH_DRAFT = "search.draft"
    SEARCH_PUBLISH = "search.publish"
    SEARCH_ACTIVATE = "search.activate"
    ACTIVITY_VIEW = "activity.view"
    ACTIVITY_RECOVER = "activity.recover"
    ACTIVITY_DISMISS = "activity.dismiss"
    ACTIVITY_REEVALUATE = "activity.reevaluate"
    ANALYTICS_VIEW = "analytics.view"
    CONTROL_VIEW = "control.view"
    CONTROL_RUN = "control.run"
    CONTROL_SCHEDULE = "control.schedule"
    ADMIN_VIEW = "admin.view"
    ADMIN_MEMBERS = "admin.members"
    ADMIN_CREDENTIALS = "admin.credentials"
    ADMIN_BUDGET = "admin.budget"


_AREA_VIEW: Mapping[Capability, Capability] = MappingProxyType(
    {
        capability: Capability(f"{capability.value.split('.')[0]}.view")
        for capability in Capability
        if capability.value.split(".")[0] in {"review", "search", "activity", "control", "admin"}
    }
)


class Preset(StrEnum):
    REVIEWER = "Reviewer"
    SEARCH_MANAGER = "Search manager"
    OPERATOR = "Operator"
    ADMIN = "Admin"


_PRESET_GRANTS: Mapping[Preset, frozenset[Capability]] = MappingProxyType(
    {
        Preset.REVIEWER: frozenset({Capability.REVIEW_VIEW, Capability.REVIEW_SUBMIT}),
        Preset.SEARCH_MANAGER: frozenset(
            {
                Capability.SEARCH_VIEW,
                Capability.SEARCH_DRAFT,
                Capability.SEARCH_PUBLISH,
                Capability.SEARCH_ACTIVATE,
            }
        ),
        Preset.OPERATOR: frozenset(
            {
                Capability.ACTIVITY_VIEW,
                Capability.ACTIVITY_RECOVER,
                Capability.ACTIVITY_DISMISS,
                Capability.ACTIVITY_REEVALUATE,
                Capability.CONTROL_VIEW,
                Capability.CONTROL_RUN,
                Capability.CONTROL_SCHEDULE,
            }
        ),
        Preset.ADMIN: frozenset(Capability),
    }
)


def preset_grants(preset: Preset) -> frozenset[Capability]:
    """Return a snapshot suitable for storing as the account's individual grants."""
    return _PRESET_GRANTS[preset]


def has_capability(grants: frozenset[Capability], capability: Capability) -> bool:
    required_view = _AREA_VIEW.get(capability)
    return capability in grants and (required_view is None or required_view in grants)


def landing_path(grants: frozenset[Capability]) -> str:
    for capability, path in (
        (Capability.REVIEW_VIEW, "/"),
        (Capability.SEARCH_VIEW, "/configuration"),
        (Capability.ACTIVITY_VIEW, "/operations/runs"),
        (Capability.ANALYTICS_VIEW, "/operations/analytics"),
        (Capability.CONTROL_VIEW, "/operations/control"),
        (Capability.ADMIN_VIEW, "/members"),
    ):
        if has_capability(grants, capability):
            return path
    return "/no-access"


class RouteAccess(StrEnum):
    PUBLIC = "public"
    LOGIN = "login"
    AUTHENTICATED = "authenticated"
    ONBOARDING = "onboarding"
    PROTECTED = "protected"


@dataclass(frozen=True)
class RoutePolicy:
    access: RouteAccess
    capability: Capability | None = None
    post_setup_capability: Capability | None = None

    def __post_init__(self) -> None:
        if (self.access is RouteAccess.PROTECTED) != (self.capability is not None):
            raise ValueError("Protected routes require one capability; other routes require none")
        if self.post_setup_capability is not None and self.access is not RouteAccess.ONBOARDING:
            raise ValueError("Post-setup requirements apply only to onboarding routes")


def _public() -> RoutePolicy:
    return RoutePolicy(RouteAccess.PUBLIC)


def _login() -> RoutePolicy:
    return RoutePolicy(RouteAccess.LOGIN)


def _authenticated() -> RoutePolicy:
    return RoutePolicy(RouteAccess.AUTHENTICATED)


def _onboarding(post_setup_capability: Capability | None = None) -> RoutePolicy:
    return RoutePolicy(RouteAccess.ONBOARDING, post_setup_capability=post_setup_capability)


def _protected(capability: Capability) -> RoutePolicy:
    return RoutePolicy(RouteAccess.PROTECTED, capability)


RouteKey = tuple[str, str]


ROUTE_POLICIES: Mapping[RouteKey, RoutePolicy] = MappingProxyType(
    {
        ("GET", "/healthz"): _public(),
        ("GET", "/readyz"): _public(),
        ("GET", "/favicon.ico"): _public(),
        ("GET", "/static/{name}"): _authenticated(),
        ("GET", "/login"): _login(),
        ("POST", "/login"): _login(),
        ("POST", "/logout"): _authenticated(),
        ("GET", "/no-access"): _authenticated(),
        ("GET", "/invite/{token}"): _public(),
        ("POST", "/invite/{token}"): _public(),
        ("GET", "/reset/{token}"): _public(),
        ("POST", "/reset/{token}"): _public(),
        ("GET", "/setup"): _onboarding(),
        ("POST", "/setup"): _onboarding(),
        ("GET", "/setup/providers"): _onboarding(Capability.ADMIN_VIEW),
        ("POST", "/setup/providers"): _onboarding(Capability.ADMIN_CREDENTIALS),
        ("POST", "/setup/providers/continue"): _onboarding(),
        ("GET", "/setup/budget"): _onboarding(Capability.ADMIN_VIEW),
        ("POST", "/setup/budget"): _onboarding(Capability.ADMIN_BUDGET),
        ("GET", "/setup/test-search"): _onboarding(Capability.SEARCH_VIEW),
        ("POST", "/setup/test-search"): _onboarding(),
        ("GET", "/members"): _protected(Capability.ADMIN_VIEW),
        ("GET", "/members/invite"): _protected(Capability.ADMIN_MEMBERS),
        ("POST", "/members/invite"): _protected(Capability.ADMIN_MEMBERS),
        ("POST", "/members/{member_id}"): _protected(Capability.ADMIN_MEMBERS),
        ("POST", "/members/{member_id}/reset"): _protected(Capability.ADMIN_MEMBERS),
        ("GET", "/"): _protected(Capability.REVIEW_VIEW),
        ("GET", "/review"): _protected(Capability.REVIEW_VIEW),
        ("GET", "/review/item/{review_item_id}"): _protected(Capability.REVIEW_VIEW),
        ("POST", "/review/{review_item_id}"): _protected(Capability.REVIEW_SUBMIT),
        ("GET", "/operations"): _authenticated(),
        ("GET", "/operations/runs"): _protected(Capability.ACTIVITY_VIEW),
        ("GET", "/operations/runs/{run_id}"): _protected(Capability.ACTIVITY_VIEW),
        ("GET", "/operations/work/{job_id}"): _protected(Capability.ACTIVITY_VIEW),
        ("GET", "/operations/failures"): _protected(Capability.ACTIVITY_VIEW),
        ("POST", "/operations/recovery"): _protected(Capability.ACTIVITY_RECOVER),
        ("POST", "/operations/dismiss"): _protected(Capability.ACTIVITY_DISMISS),
        ("POST", "/operations/reevaluation"): _protected(Capability.ACTIVITY_REEVALUATE),
        ("GET", "/operations/analytics"): _protected(Capability.ANALYTICS_VIEW),
        ("GET", "/operations/control"): _protected(Capability.CONTROL_VIEW),
        ("POST", "/operations/run"): _protected(Capability.CONTROL_RUN),
        ("POST", "/operations/schedule"): _protected(Capability.CONTROL_SCHEDULE),
        ("GET", "/configuration"): _protected(Capability.SEARCH_VIEW),
        ("POST", "/configuration/acquisition/draft"): _protected(Capability.SEARCH_DRAFT),
        ("POST", "/configuration/acquisition/publish"): _protected(Capability.SEARCH_PUBLISH),
        ("POST", "/configuration/acquisition/activate"): _protected(Capability.SEARCH_ACTIVATE),
        ("POST", "/configuration/qualification/draft"): _protected(Capability.SEARCH_DRAFT),
        ("POST", "/configuration/qualification/publish"): _protected(Capability.SEARCH_PUBLISH),
        ("POST", "/configuration/continue"): _onboarding(),
        ("GET", "/configuration/qualification-targets"): _protected(Capability.SEARCH_VIEW),
        ("POST", "/configuration/qualification-targets/candidate"): _protected(
            Capability.SEARCH_DRAFT
        ),
        ("GET", "/configuration/qualification-promotion"): _protected(Capability.SEARCH_VIEW),
        ("POST", "/configuration/qualification-promotion/preview"): _protected(
            Capability.SEARCH_VIEW
        ),
        ("POST", "/configuration/qualification-promotion/decide"): _protected(
            Capability.SEARCH_PUBLISH
        ),
        ("POST", "/configuration/qualification-promotion/activate"): _protected(
            Capability.SEARCH_ACTIVATE
        ),
    }
)


def route_policy(method: str, path_template: str) -> RoutePolicy | None:
    if method == "HEAD":
        method = "GET"
    return ROUTE_POLICIES.get((method, path_template))
