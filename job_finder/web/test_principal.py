from uuid import UUID

import pytest
from starlette.requests import Request

from job_finder.review.accounts import Account, AccountRole, AccountStatus
from job_finder.web.principal import actor_email, current_account


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_actor_comes_from_each_requests_current_account() -> None:
    first = _request()
    second = _request()
    first.state.principal = Account(
        UUID(int=1), "first@example.com", AccountRole.MEMBER, AccountStatus.ACTIVE, frozenset()
    )
    second.state.principal = Account(
        UUID(int=2), "second@example.com", AccountRole.ADMIN, AccountStatus.ACTIVE, frozenset()
    )

    assert actor_email(first) == "first@example.com"
    assert actor_email(second) == "second@example.com"
    assert current_account(first).id == UUID(int=1)


def test_missing_principal_cannot_become_an_action_actor() -> None:
    with pytest.raises(RuntimeError, match="authenticated account required"):
        actor_email(_request())
