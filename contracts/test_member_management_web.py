from __future__ import annotations

import re
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from socket import socket
from typing import cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fasthtml.common import FastHTML
from psycopg import sql
from pydantic import SecretStr
from starlette.testclient import TestClient
from starlette.routing import Route

from job_finder.access_policy import Capability, RouteAccess, route_policy
from job_finder.config import PostgresContractSettings, ReviewAppSettings
from job_finder.database import ConnectionFactory, apply_migrations
from job_finder.review.accounts import AccountRole, AccountService
from job_finder.review.owner_access import OnboardingStage, OwnerAccessService, OwnerAccessState
from job_finder.review.queue import ReviewQueue
from job_finder.review.test_app_support import OWNER_EMAIL, OWNER_PASSWORD, FakeAccountService
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


def _login_response(
    client: TestClient, email: str, password: str, next_url: str = "/"
) -> httpx2.Response:
    form = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf_token": _csrf(form.text),
            "email": email,
            "password": password,
            "next": next_url,
        },
        follow_redirects=False,
    )


def _login(client: TestClient, email: str, password: str, next_url: str = "/") -> int:
    return _login_response(client, email, password, next_url).status_code


def _accept_member(
    accounts: AccountService, admin_id: UUID, email: str, grants: frozenset[Capability]
) -> None:
    invite = accounts.issue_invite(admin_id, email, role=AccountRole.MEMBER, grants=grants)
    assert accounts.accept_invite(invite, "secure-member-password") is not None


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


def test_login_lands_on_the_first_granted_area(
    member_app: tuple[TestClient, AccountService],
) -> None:
    admin_client, accounts = member_app
    admin = next(user for user in accounts.list_members() if user.email == "admin@example.com")

    _accept_member(
        accounts,
        admin.id,
        "reviewer@example.com",
        frozenset({Capability.REVIEW_VIEW, Capability.REVIEW_SUBMIT}),
    )
    reviewer = TestClient(admin_client.app)
    reviewer_response = _login_response(reviewer, "reviewer@example.com", "secure-member-password")
    assert reviewer_response.status_code == 303
    assert reviewer_response.headers["location"] == "/"

    _accept_member(accounts, admin.id, "none@example.com", frozenset())
    ungranted = TestClient(admin_client.app)
    ungranted_response = _login_response(ungranted, "none@example.com", "secure-member-password")
    assert ungranted_response.status_code == 303
    assert ungranted_response.headers["location"] == "/no-access"
    no_access = ungranted.get("/no-access")
    assert no_access.status_code == 200
    assert "No access assigned" in no_access.text
    assert "Ask an administrator to grant access to an area." in no_access.text

    _accept_member(
        accounts, admin.id, "analyst@example.com", frozenset({Capability.ANALYTICS_VIEW})
    )
    analyst = TestClient(admin_client.app)
    analyst_response = _login_response(analyst, "analyst@example.com", "secure-member-password")
    assert analyst_response.status_code == 303
    assert analyst_response.headers["location"] == "/operations/analytics"


def test_members_page_is_read_only_without_the_members_grant(
    member_app: tuple[TestClient, AccountService],
) -> None:
    admin_client, accounts = member_app
    admin = next(user for user in accounts.list_members() if user.email == "admin@example.com")
    _accept_member(accounts, admin.id, "viewer@example.com", frozenset({Capability.ADMIN_VIEW}))

    assert _login(admin_client, "admin@example.com", "secure-admin-password") == 303
    admin_page = admin_client.get("/members")
    assert admin_page.status_code == 200
    assert "Invite a member" in admin_page.text
    assert 'action="/members/' in admin_page.text

    viewer = TestClient(admin_client.app)
    assert _login(viewer, "viewer@example.com", "secure-member-password") == 303
    page = viewer.get("/members")
    assert page.status_code == 200
    assert "admin@example.com" in page.text
    assert "viewer@example.com" in page.text
    assert "Invite a member" not in page.text
    assert 'action="/members/' not in page.text


def test_acceptance_forms_reject_mismatched_and_short_passwords(
    member_app: tuple[TestClient, AccountService],
) -> None:
    admin_client, accounts = member_app
    admin = next(user for user in accounts.list_members() if user.email == "admin@example.com")

    invite = accounts.issue_invite(
        admin.id, "member@example.com", role=AccountRole.MEMBER, grants=frozenset()
    )
    recipient = TestClient(admin_client.app)
    invite_form = recipient.get(f"/invite/{invite}")
    invite_csrf = {"csrf_token": _csrf(invite_form.text)}
    mismatched = recipient.post(
        f"/invite/{invite}",
        data={
            **invite_csrf,
            "password": "secure-member-password",
            "password_confirmation": "differing-password",
        },
    )
    assert mismatched.status_code == 400
    assert "Passwords differ." in mismatched.text
    short = recipient.post(
        f"/invite/{invite}",
        data={**invite_csrf, "password": "shortpw1", "password_confirmation": "shortpw1"},
    )
    assert short.status_code == 400
    assert "Use a password of 12 to 1024 characters." in short.text
    accepted = recipient.post(
        f"/invite/{invite}",
        data={
            **invite_csrf,
            "password": "secure-member-password",
            "password_confirmation": "secure-member-password",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    member = next(user for user in accounts.list_members() if user.email == "member@example.com")
    reset = accounts.issue_reset(admin.id, member.id)
    holder = TestClient(admin_client.app)
    reset_form = holder.get(f"/reset/{reset}")
    reset_csrf = {"csrf_token": _csrf(reset_form.text)}
    mismatched_reset = holder.post(
        f"/reset/{reset}",
        data={
            **reset_csrf,
            "password": "new-secure-password",
            "password_confirmation": "differing-password",
        },
    )
    assert mismatched_reset.status_code == 400
    assert "Passwords differ." in mismatched_reset.text
    short_reset = holder.post(
        f"/reset/{reset}",
        data={**reset_csrf, "password": "shortpw1", "password_confirmation": "shortpw1"},
    )
    assert short_reset.status_code == 400
    assert "Use a password of 12 to 1024 characters." in short_reset.text
    accepted_reset = holder.post(
        f"/reset/{reset}",
        data={
            **reset_csrf,
            "password": "new-secure-password",
            "password_confirmation": "new-secure-password",
        },
        follow_redirects=False,
    )
    assert accepted_reset.status_code == 303


def test_duplicate_invite_via_the_web_reports_the_conflict(
    member_app: tuple[TestClient, AccountService],
) -> None:
    admin_client, accounts = member_app
    assert _login(admin_client, "admin@example.com", "secure-admin-password", "/members") == 303
    invite_form = admin_client.get("/members/invite")
    data = {
        "csrf_token": _csrf(invite_form.text),
        "email": "reviewer@example.com",
        "preset": "Reviewer",
        "grants": ["review.view", "review.submit"],
    }
    invited = admin_client.post("/members/invite", data=data)
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
    duplicate = admin_client.post("/members/invite", data=data)
    assert duplicate.status_code == 400
    assert "account already exists" in duplicate.text
    assert "/invite/" not in duplicate.text


def _refused_postgres_dsn() -> str:
    with socket() as probe:
        probe.bind(("127.0.0.1", 0))
        _host, port = cast(tuple[str, int], probe.getsockname())
    return f"postgresql://redact:hunter2@127.0.0.1:{port}/test"


class _OutageAccountService(FakeAccountService, AccountService):
    def __init__(self, connect: ConnectionFactory) -> None:
        FakeAccountService.__init__(self)
        AccountService.__init__(self, connect=connect)


def _outage_client() -> TestClient:
    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(_refused_postgres_dsn(), autocommit=True)

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
        account_service=_OutageAccountService(connect),
    )
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path", "fields", "unavailable"),
    [
        ("GET", "/members", None, "Members unavailable"),
        (
            "POST",
            "/members/invite",
            {
                "email": "outage@example.com",
                "preset": "Reviewer",
                "grants": ["review.view", "review.submit"],
            },
            "Invite unavailable",
        ),
        (
            "POST",
            f"/members/{uuid4()}",
            {"role": "member", "status": "active"},
            "Change unavailable",
        ),
        ("POST", f"/members/{uuid4()}/reset", {}, "Reset unavailable"),
        (
            "POST",
            f"/invite/{'t' * 43}",
            {
                "password": "secure-member-password",
                "password_confirmation": "secure-member-password",
            },
            "Invitation unavailable",
        ),
        (
            "POST",
            f"/reset/{'t' * 43}",
            {
                "password": "secure-member-password",
                "password_confirmation": "secure-member-password",
            },
            "Reset unavailable",
        ),
    ],
)
def test_member_routes_report_a_database_outage_with_distinct_copy(
    method: str,
    path: str,
    fields: dict[str, str | list[str]] | None,
    unavailable: str,
) -> None:
    client = _outage_client()
    assert _login(client, OWNER_EMAIL, OWNER_PASSWORD) == 303
    home = client.get("/")
    assert home.status_code == 200

    if method == "GET":
        response = client.get(path)
    else:
        response = client.post(path, data={"csrf_token": _csrf(home.text), **(fields or {})})

    assert response.status_code == 503
    assert unavailable in response.text
    assert "Reload and try again." in response.text
    assert "hunter2" not in response.text
