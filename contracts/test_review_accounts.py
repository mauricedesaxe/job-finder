from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
import re
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.review.accounts import AccountRole, AccountService, AccountStatus
from job_finder.access_policy import Capability


@pytest.fixture
def account_db() -> Iterator[tuple[str, str]]:
    dsn = PostgresContractSettings.from_environment().postgres_dsn
    schema = f"job_finder_accounts_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            apply_migrations(connection)
            yield dsn, schema
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _service(dsn: str, schema: str) -> AccountService:
    @contextmanager
    def connect() -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            yield connection

    return AccountService(connect)


def test_session_and_invite_are_live_and_one_use(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    admin = service.first_admin("ADMIN@example.com", "secure-first-password")
    assert admin is not None
    assert service.first_admin("another@example.com", "secure-first-password") is None
    session = service.create_session(admin.id)
    assert service.load_principal(session) == admin
    invite = service.issue_invite(
        admin.id,
        "member@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.REVIEW_VIEW, Capability.REVIEW_SUBMIT}),
    )
    member = service.accept_invite(invite, "secure-member-password")
    assert member is not None
    assert service.accept_invite(invite, "secure-member-password") is None
    assert service.authenticate("MEMBER@example.com", "secure-member-password") == member
    member_session = service.create_session(member.id)
    service.set_member(
        admin.id,
        member.id,
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ANALYTICS_VIEW}),
        status=AccountStatus.ACTIVE,
    )
    refreshed = service.load_principal(member_session)
    assert refreshed is not None
    assert refreshed.capabilities == frozenset({Capability.ANALYTICS_VIEW})
    service.set_member(
        admin.id,
        member.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.DISABLED,
    )
    assert service.load_principal(member_session) is None
    assert service.authenticate("member@example.com", "secure-member-password") is None
    service.set_member(
        admin.id,
        member.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.ACTIVE,
    )
    assert service.load_principal(member_session) is None
    service.revoke_session(session)
    assert service.load_principal(session) is None


def test_reset_consumption_revokes_sessions(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    admin = service.first_admin("admin@example.com", "secure-first-password")
    assert admin is not None
    session = service.create_session(admin.id)
    reset = service.issue_reset(admin.id, admin.id)
    assert service.accept_reset(reset, "new-secure-password")
    assert not service.accept_reset(reset, "another-secure-password")
    assert service.load_principal(session) is None
    assert service.authenticate("admin@example.com", "new-secure-password") == admin


def test_invite_requires_issuer_to_retain_membership_authority(
    account_db: tuple[str, str],
) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    admin = service.first_admin("admin@example.com", "secure-first-password")
    assert admin is not None
    manager_link = service.issue_invite(
        admin.id,
        "manager@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ADMIN_VIEW, Capability.ADMIN_MEMBERS}),
    )
    manager = service.accept_invite(manager_link, "secure-manager-password")
    assert manager is not None
    pending = service.issue_invite(
        manager.id,
        "pending@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ANALYTICS_VIEW}),
    )
    service.set_member(
        admin.id,
        manager.id,
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ADMIN_VIEW}),
        status=AccountStatus.ACTIVE,
    )
    assert service.accept_invite(pending, "secure-pending-password") is None
    service.set_member(
        admin.id,
        manager.id,
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ADMIN_VIEW, Capability.ADMIN_MEMBERS}),
        status=AccountStatus.ACTIVE,
    )
    pending_disabled = service.issue_invite(
        manager.id,
        "disabled-inviter@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset(),
    )
    service.set_member(
        admin.id,
        manager.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.DISABLED,
    )
    assert service.accept_invite(pending_disabled, "secure-pending-password") is None
    assert service.authenticate("pending@example.com", "secure-pending-password") is None


def test_admin_invite_requires_issuer_to_remain_admin(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    first = service.first_admin("first@example.com", "secure-first-password")
    assert first is not None
    second_link = service.issue_invite(
        first.id, "second@example.com", role=AccountRole.ADMIN, grants=frozenset()
    )
    second = service.accept_invite(second_link, "secure-second-password")
    assert second is not None
    pending = service.issue_invite(
        first.id, "third@example.com", role=AccountRole.ADMIN, grants=frozenset()
    )
    service.set_member(
        second.id,
        first.id,
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ADMIN_VIEW, Capability.ADMIN_MEMBERS}),
        status=AccountStatus.ACTIVE,
    )
    assert service.accept_invite(pending, "secure-third-password") is None


def test_reset_requires_authorized_issuer_and_active_subject(
    account_db: tuple[str, str],
) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    admin = service.first_admin("admin@example.com", "secure-first-password")
    assert admin is not None
    invite = service.issue_invite(
        admin.id, "member@example.com", role=AccountRole.MEMBER, grants=frozenset()
    )
    member = service.accept_invite(invite, "secure-member-password")
    assert member is not None
    pending = service.issue_reset(admin.id, member.id)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute(
            "UPDATE review_account_tokens SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE purpose = 'password_reset'"
        )
    assert not service.accept_reset(pending, "new-secure-password")
    pending = service.issue_reset(admin.id, member.id)
    service.set_member(
        admin.id,
        member.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.DISABLED,
    )
    assert not service.accept_reset(pending, "new-secure-password")
    with pytest.raises(ValueError, match="active member"):
        service.issue_reset(admin.id, member.id)
    service.set_member(
        admin.id,
        member.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.ACTIVE,
    )
    pending = service.issue_reset(admin.id, member.id)
    second_link = service.issue_invite(
        admin.id, "second@example.com", role=AccountRole.ADMIN, grants=frozenset()
    )
    second = service.accept_invite(second_link, "secure-second-password")
    assert second is not None
    service.set_member(
        second.id,
        admin.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.DISABLED,
    )
    assert not service.accept_reset(pending, "new-secure-password")
    assert service.authenticate("member@example.com", "secure-member-password") == member


def test_last_admin_guard_serializes_direct_sql(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    first = service.first_admin("first@example.com", "secure-first-password")
    assert first is not None
    invite = service.issue_invite(
        first.id, "second@example.com", role=AccountRole.ADMIN, grants=frozenset()
    )
    second = service.accept_invite(invite, "secure-second-password")
    assert second is not None

    def disable(user_id: object) -> bool:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            try:
                with connection.transaction():
                    barrier.wait(timeout=5)
                    connection.execute(
                        "UPDATE review_users SET status = 'disabled' WHERE id = %s", (user_id,)
                    )
            except psycopg.errors.CheckViolation:
                return False
            return True

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(disable, (first.id, second.id)))
    assert sorted(results) == [False, True]
    assert (
        len(
            [
                user
                for user in service.list_members()
                if user.role is AccountRole.ADMIN and user.status is AccountStatus.ACTIVE
            ]
        )
        == 1
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        remaining = next(
            user for user in service.list_members() if user.status is AccountStatus.ACTIVE
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="last active admin"):
            connection.execute("DELETE FROM review_users WHERE id = %s", (remaining.id,))


def test_expired_links_and_audit_immutability(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    admin = service.first_admin("admin@example.com", "secure-first-password")
    assert admin is not None
    invite = service.issue_invite(
        admin.id,
        "member@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ANALYTICS_VIEW}),
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute(
            "UPDATE review_account_tokens SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'"
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
            connection.execute("DELETE FROM review_membership_audit")
    assert service.accept_invite(invite, "secure-member-password") is None


def test_first_admin_claim_is_atomic(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    barrier = Barrier(2)

    def claim(email: str) -> object:
        barrier.wait(timeout=5)
        return service.first_admin(email, "secure-first-password")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("first@example.com", "second@example.com")))
    assert sum(claim is not None for claim in claims) == 1
    assert len(service.list_members()) == 1
    assert service.has_accounts()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        row = connection.execute(
            "SELECT stage, password_hash FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone()
    assert row == ("providers", None)


@pytest.mark.parametrize(
    "claim_stage", ("providers", "preferences", "budget", "test_search", "complete")
)
def test_first_admin_requires_legacy_password_when_present(
    account_db: tuple[str, str], claim_stage: str
) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    from job_finder.review.owner_access import hash_owner_password

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute(
            "UPDATE owner_onboarding SET stage = 'providers', password_hash = %s WHERE singleton_id = 1",
            (hash_owner_password("legacy-owner-password"),),
        )
        with pytest.raises(PermissionError, match="legacy owner password"):
            service.first_admin("admin@example.com", "new-admin-password")
        for stage in ("preferences", "budget", "test_search", "complete"):
            if claim_stage == "providers":
                break
            connection.execute(
                "UPDATE owner_onboarding SET stage = %s WHERE singleton_id = 1", (stage,)
            )
            if stage == claim_stage:
                break
    with pytest.raises(PermissionError, match="legacy owner password"):
        service.first_admin("admin@example.com", "new-admin-password")
    with pytest.raises(PermissionError, match="legacy owner password"):
        service.first_admin(
            "admin@example.com", "new-admin-password", legacy_password="wrong-password"
        )
    assert (
        service.first_admin(
            "admin@example.com", "new-admin-password", legacy_password="legacy-owner-password"
        )
        is not None
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        row = connection.execute(
            "SELECT stage, password_hash FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone()
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE owner_onboarding SET password_hash = %s WHERE singleton_id = 1",
                (hash_owner_password("restored-shared-password"),),
            )
    assert row == (claim_stage, None)


def test_sql_grant_allowlist_matches_capability_registry() -> None:
    migration = (
        Path(__file__).parents[1] / "job_finder/migrations/0052_review_accounts.sql"
    ).read_text()
    allowlist = migration.split("CHECK (grants <@ ARRAY[", 1)[1].split("]::TEXT[])", 1)[0]
    assert set(re.findall(r"'([a-z]+\.[a-z]+)'", allowlist)) == {cap.value for cap in Capability}


def test_member_manager_can_manage_members_but_not_admins(account_db: tuple[str, str]) -> None:
    dsn, schema = account_db
    service = _service(dsn, schema)
    admin = service.first_admin("admin@example.com", "secure-first-password")
    assert admin is not None
    manager_invite = service.issue_invite(
        admin.id,
        "manager@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ADMIN_VIEW, Capability.ADMIN_MEMBERS}),
    )
    manager = service.accept_invite(manager_invite, "secure-manager-password")
    assert manager is not None
    invite = service.issue_invite(
        manager.id,
        "member@example.com",
        role=AccountRole.MEMBER,
        grants=frozenset({Capability.ANALYTICS_VIEW}),
    )
    member = service.accept_invite(invite, "secure-member-password")
    assert member is not None
    service.set_member(
        manager.id,
        member.id,
        role=AccountRole.MEMBER,
        grants=frozenset(),
        status=AccountStatus.ACTIVE,
    )
    with pytest.raises(PermissionError, match="admin role"):
        service.set_member(
            manager.id,
            member.id,
            role=AccountRole.ADMIN,
            grants=frozenset(),
            status=AccountStatus.ACTIVE,
        )
    with pytest.raises(PermissionError, match="admin invitations"):
        service.issue_invite(
            manager.id, "another@example.com", role=AccountRole.ADMIN, grants=frozenset()
        )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        rows = connection.execute(
            "SELECT detail ->> 'email' FROM review_membership_audit WHERE action = 'invited'"
        ).fetchall()
    assert {row[0] for row in rows} == {"manager@example.com", "member@example.com"}
