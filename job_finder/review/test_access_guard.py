from __future__ import annotations

from typing import NoReturn
from uuid import UUID

import psycopg
import pytest
from starlette.requests import Request

from job_finder.access_policy import Capability, RouteAccess, RoutePolicy
from job_finder.review.access_guard import require_account
from job_finder.review.accounts import Account, AccountRole, AccountService, AccountStatus
from job_finder.review.owner_access import OnboardingStage, OwnerAccessService, OwnerAccessState


def _request(
    path: str, session: dict[str, object] | None = None, *, method: str = "GET"
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "session": session or {},
        }
    )


def _owner(
    stage: OnboardingStage = OnboardingStage.COMPLETE, *, hash_present: bool = False
) -> OwnerAccessService:
    return OwnerAccessService(
        load_state=lambda: OwnerAccessState(stage, hash_present),
        authenticate=lambda _password: False,
        bootstrap=lambda _password: pytest.fail("unexpected bootstrap"),
    )


def _no_database() -> NoReturn:
    raise RuntimeError("no database in this test")


class StubAccountService(AccountService):
    _present: bool = True
    _principal: Account | None = None
    _session_error: psycopg.Error | None = None

    def __init__(
        self,
        *,
        has_accounts: bool = True,
        principal: Account | None = None,
        session_error: psycopg.Error | None = None,
    ) -> None:
        super().__init__(_no_database)
        object.__setattr__(self, "_present", has_accounts)
        object.__setattr__(self, "_principal", principal)
        object.__setattr__(self, "_session_error", session_error)

    def has_accounts(self) -> bool:
        return self._present

    def load_principal(self, token: str) -> Account | None:
        if self._session_error is not None:
            raise self._session_error
        return self._principal


def _accounts(principal: Account | None, *, has_accounts: bool = True) -> AccountService:
    return StubAccountService(principal=principal, has_accounts=has_accounts)


def test_old_owner_cookie_cannot_authorize_review() -> None:
    accounts = _accounts(None)
    response = require_account(
        _request("/", {"authenticated": True, "csrf_token": "old"}),
        _owner(),
        accounts,
        None,
        RoutePolicy(RouteAccess.PROTECTED, Capability.REVIEW_VIEW),
    )
    assert response is not None and response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_member_must_have_route_capability() -> None:
    member = Account(
        UUID(int=1),
        "reviewer@example.com",
        AccountRole.MEMBER,
        AccountStatus.ACTIVE,
        frozenset({Capability.REVIEW_VIEW}),
    )
    accounts = _accounts(member)
    denied = require_account(
        _request("/review/1", {"account_session": "token"}),
        _owner(),
        accounts,
        None,
        RoutePolicy(RouteAccess.PROTECTED, Capability.REVIEW_SUBMIT),
    )
    assert denied is not None and denied.status_code == 403
    allowed = require_account(
        _request("/review", {"account_session": "token"}),
        _owner(),
        accounts,
        None,
        RoutePolicy(RouteAccess.PROTECTED, Capability.REVIEW_VIEW),
    )
    assert allowed is None


def test_unclaimed_legacy_owner_reaches_claim_page() -> None:
    accounts = _accounts(None, has_accounts=False)
    response = require_account(
        _request("/setup", {"authenticated": True}),
        _owner(hash_present=True),
        accounts,
        None,
        RoutePolicy(RouteAccess.ONBOARDING),
    )
    assert response is None


def test_onboarding_search_actions_still_require_their_capability() -> None:
    member = Account(
        UUID(int=2),
        "reviewer@example.com",
        AccountRole.MEMBER,
        AccountStatus.ACTIVE,
        frozenset({Capability.SEARCH_VIEW}),
    )
    response = require_account(
        _request("/configuration/acquisition/publish", {"account_session": "token"}, method="POST"),
        _owner(OnboardingStage.PREFERENCES),
        _accounts(member),
        None,
        RoutePolicy(RouteAccess.PROTECTED, Capability.SEARCH_PUBLISH),
    )
    assert response is not None and response.status_code == 403


def test_completed_provider_update_needs_credential_grant() -> None:
    member = Account(
        UUID(int=3),
        "manager@example.com",
        AccountRole.MEMBER,
        AccountStatus.ACTIVE,
        frozenset({Capability.ADMIN_VIEW}),
    )
    response = require_account(
        _request("/setup/providers", {"account_session": "token"}, method="POST"),
        _owner(),
        _accounts(member),
        None,
        RoutePolicy(RouteAccess.ONBOARDING, post_setup_capability=Capability.ADMIN_CREDENTIALS),
    )
    assert response is not None and response.status_code == 403


def test_reports_a_session_check_database_failure() -> None:
    accounts = StubAccountService(
        principal=None, session_error=psycopg.OperationalError("connection refused")
    )

    response = require_account(
        _request("/review", {"account_session": "token"}),
        _owner(),
        accounts,
        None,
        RoutePolicy(RouteAccess.PROTECTED, Capability.REVIEW_VIEW),
    )

    assert response is not None and response.status_code == 503
    assert "Your session could not be checked" in bytes(response.body).decode()


def test_answers_a_path_with_a_parent_segment_with_not_found() -> None:
    response = require_account(
        _request("/static/../app.py", {"account_session": "token"}),
        _owner(),
        _accounts(None),
        None,
        RoutePolicy(RouteAccess.PROTECTED, Capability.REVIEW_VIEW),
    )

    assert response is not None and response.status_code == 404
