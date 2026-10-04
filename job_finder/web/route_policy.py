# pyright: reportMissingTypeStubs=false
"""Bind the framework's registered routes to the capability policy."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from fasthtml.common import FastHTML
from starlette.routing import Route

from job_finder.access_policy import ROUTE_POLICIES, RouteKey, RoutePolicy


def compile_route_policies(
    app: FastHTML, *, review_routes: bool, split_routes: bool = False
) -> Mapping[tuple[object, str], RoutePolicy]:
    """Validate registered routes and bind policy to the selected endpoint and method."""
    expected = _expected_routes(review_routes=review_routes, split_routes=split_routes)
    bound: dict[tuple[object, str], RoutePolicy] = {}
    actual: set[RouteKey] = set()
    for route in app.routes:
        if not isinstance(route, Route) or route.methods is None:
            raise ValueError(f"Unclassified route type: {route!r}")
        route_keys = {_route_key(method, route.path) for method in route.methods}
        duplicates = actual & route_keys
        if duplicates:
            raise ValueError(f"Duplicate routes: {duplicates}")
        actual.update(route_keys)
        for method in route.methods:
            key = _route_key(method, route.path)
            policy = ROUTE_POLICIES.get(key)
            if policy is None:
                raise ValueError(f"Route has no access policy: {key}")
            bound[(route.endpoint, method)] = policy
    if actual != expected:
        raise ValueError(
            f"Route policy mismatch; missing={expected - actual}, extra={actual - expected}"
        )
    return MappingProxyType(bound)


def _expected_routes(*, review_routes: bool, split_routes: bool) -> set[RouteKey]:
    split_routes_only = frozenset(
        key for key in ROUTE_POLICIES if key[1].startswith("/configuration")
    )
    return (
        set(ROUTE_POLICIES)
        if review_routes and split_routes
        else set(ROUTE_POLICIES) - split_routes_only
        if review_routes
        else {
            key
            for key in ROUTE_POLICIES
            if key[1] in {"/healthz", "/readyz", "/favicon.ico", "/static/{name}"}
        }
    )


def _route_key(method: str, path: str) -> RouteKey:
    return ("GET" if method == "HEAD" else method, path)
