from __future__ import annotations

import re
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import cast
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from fasthtml.common import FastHTML
from psycopg import sql
from pydantic import SecretStr
from starlette.testclient import TestClient
from starlette.routing import Route

from job_finder.access_policy import RouteAccess, route_policy
from job_finder.config import PostgresContractSettings, ReviewAppSettings
from job_finder.database import apply_migrations
from job_finder.review.accounts import AccountRole, AccountService
from job_finder.review.owner_access import OnboardingStage, OwnerAccessService, OwnerAccessState
from job_finder.review.queue import ReviewQueue
from job_finder.web.app import create_review_app


@pytest.fixture
def member_app() -> Iterator[tuple[TestClient, AccountService]]:
    dsn = PostgresContractSettings.from_environment().postgres_dsn
    schema = f"job_finder_member_web_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            apply_migrations(connection)

            @contextmanager
            def connect() -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
                with psycopg.connect(dsn, autocommit=True) as scoped:
                    scoped.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    yield scoped

            accounts = AccountService(connect)
            assert accounts.first_admin("admin@example.com", "secure-admin-password") is not None
            owner_state = OwnerAccessState(OnboardingStage.COMPLETE, False)
            owner_access = OwnerAccessService(
                load_state=lambda: owner_state,
                authenticate=lambda _password: False,
                bootstrap=lambda _password: pytest.fail("unexpected bootstrap"),
            )
            settings = ReviewAppSettings(
                bootstrap_token=SecretStr("bootstrap-token-with-at-least-32-characters"),
                session_secret="s" * 32,
                cookie_secure=False,
            )
            app = create_review_app(
                lambda: ReviewQueue(items=()),
                settings,
                submit_review=lambda _review: pytest.fail("unexpected review"),
                owner_access_service=owner_access,
                account_service=accounts,
            )
            yield TestClient(app), accounts
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _link(response_text: str, kind: str) -> str:
    match = re.search(rf'value="(http://testserver/{kind}/[^"]+)"', response_text)
    assert match is not None
    return urlparse(match.group(1)).path


def _login(client: TestClient, email: str, password: str, next_url: str = "/") -> int:
    form = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": _csrf(form.text),
            "email": email,
            "password": password,
            "next": next_url,
        },
        follow_redirects=False,
    )
    return response.status_code


def test_member_invite_grants_and_disable_are_enforced(
    member_app: tuple[TestClient, AccountService],
) -> None:
    admin_client, accounts = member_app
    assert _login(admin_client, "admin@example.com", "secure-admin-password", "/members") == 303
    invite_form = admin_client.get("/members/invite")
    assert invite_form.status_code == 200
    invited = admin_client.post(
        "/members/invite",
        data={
            "csrf_token": _csrf(invite_form.text),
            "email": "reviewer@example.com",
            "preset": "Reviewer",
            "grants": ["review.view", "review.submit"],
        },
    )
    assert invited.status_code == 200
    link = _link(invited.text, "invite")

    member_client = TestClient(admin_client.app)
    invitation = member_client.get(link)
    accepted = member_client.post(
        link,
        data={
            "csrf_token": _csrf(invitation.text),
            "password": "secure-member-password",
            "password_confirmation": "secure-member-password",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert (
        member_client.post(
            link,
            data={
                "csrf_token": _csrf(invitation.text),
                "password": "secure-member-password",
                "password_confirmation": "secure-member-password",
            },
        ).status_code
        == 410
    )
    assert _login(member_client, "reviewer@example.com", "secure-member-password") == 303
    assert member_client.get("/").status_code == 200
    assert member_client.get("/members").status_code == 403
    assert member_client.post("/operations/run").status_code == 403

    member = next(user for user in accounts.list_members() if user.email == "reviewer@example.com")
    members_page = admin_client.get("/members")
    disabled = admin_client.post(
        f"/members/{member.id}",
        data={"csrf_token": _csrf(members_page.text), "role": "member", "status": "disabled"},
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    revoked = member_client.get("/", follow_redirects=False)
    assert revoked.status_code == 303
    assert revoked.headers["location"].startswith("/login")


def test_password_reset_link_is_one_use(member_app: tuple[TestClient, AccountService]) -> None:
    client, accounts = member_app
    admin = next(user for user in accounts.list_members() if user.email == "admin@example.com")
    assert _login(client, admin.email, "secure-admin-password") == 303
    page = client.get("/members")
    blocked = client.post(
        f"/members/{admin.id}",
        data={"csrf_token": _csrf(page.text), "role": "admin", "status": "disabled"},
    )
    assert blocked.status_code == 409
    assert "Assign another admin" in blocked.text
    assert (
        next(user for user in accounts.list_members() if user.id == admin.id).status.value
        == "active"
    )
    issued = client.post(f"/members/{admin.id}/reset", data={"csrf_token": _csrf(page.text)})
    assert issued.status_code == 200
    link = _link(issued.text, "reset")
    recipient = TestClient(client.app)
    form = recipient.get(link)
    data = {
        "csrf_token": _csrf(form.text),
        "password": "new-secure-password",
        "password_confirmation": "new-secure-password",
    }
    assert recipient.post(link, data=data, follow_redirects=False).status_code == 303
    assert recipient.post(link, data=data).status_code == 410
    assert client.get("/members", follow_redirects=False).status_code == 303
    assert _login(recipient, admin.email, "new-secure-password") == 303


def test_every_registered_protected_route_denies_an_ungranted_account(
    member_app: tuple[TestClient, AccountService],
) -> None:
    admin_client, accounts = member_app
    admin = next(user for user in accounts.list_members() if user.role is AccountRole.ADMIN)
    invite = accounts.issue_invite(
        admin.id, "no-access@example.com", role=AccountRole.MEMBER, grants=frozenset()
    )
    assert accounts.accept_invite(invite, "secure-member-password") is not None
    client = TestClient(admin_client.app)
    assert _login(client, "no-access@example.com", "secure-member-password") == 303
    checked: set[tuple[str, str]] = set()
    for route in cast(FastHTML, client.app).routes:
        assert isinstance(route, Route)
        for method in route.methods or ():
            if method == "HEAD":
                continue
            policy = route_policy(method, route.path)
            assert policy is not None
            if policy.access is not RouteAccess.PROTECTED:
                continue
            path = re.sub(r"\{[^}]+\}", str(uuid4()), route.path)
            response = client.request(method, path, follow_redirects=False)
            assert response.status_code == 403, (method, route.path, response.status_code)
            checked.add((method, route.path))
    assert len(checked) >= 20
