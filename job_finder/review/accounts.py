"""Installation-local accounts, grants, sessions, and one-use account links."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from job_finder.database import Connection, ConnectionFactory
from job_finder.review.owner_access import hash_owner_password, verify_owner_password
from job_finder.access_policy import Capability, has_capability


class AccountRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True)
class Account:
    id: UUID
    email: str
    role: AccountRole
    status: AccountStatus
    grants: frozenset[Capability]

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(Capability) if self.role is AccountRole.ADMIN else self.grants


@dataclass(frozen=True)
class AccountService:
    connect: ConnectionFactory

    def has_accounts(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM review_users LIMIT 1").fetchone() is not None

    def first_admin(
        self, email: str, password: str, *, legacy_password: str | None = None
    ) -> Account | None:
        encoded = hash_owner_password(password)
        normalized = _email(email)
        with self.connect() as connection, connection.transaction():
            _lock_membership(connection)
            if connection.execute("SELECT 1 FROM review_users LIMIT 1").fetchone():
                return None
            owner_row = connection.execute(
                "SELECT stage, password_hash FROM owner_onboarding WHERE singleton_id = 1"
            ).fetchone()
            if owner_row is None:
                raise RuntimeError("owner onboarding state is missing")
            stage, old_hash = str(owner_row[0]), owner_row[1]
            if stage == "owner_account" and old_hash is None:
                pass
            elif (
                stage in {"providers", "preferences", "budget", "test_search", "complete"}
                and old_hash is not None
            ):
                if legacy_password is None or not verify_owner_password(
                    legacy_password, str(old_hash)
                ):
                    raise PermissionError("legacy owner password required")
            else:
                raise PermissionError("first admin claim unavailable at this onboarding stage")
            user_id = uuid4()
            _ = connection.execute(
                "INSERT INTO review_users (id, email, password_hash, role) VALUES (%s, %s, %s, 'admin')",
                (user_id, normalized, encoded),
            )
            if stage == "owner_account":
                _ = connection.execute(
                    """UPDATE owner_onboarding SET stage = 'providers',
                       updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1"""
                )
            else:
                _ = connection.execute(
                    """UPDATE owner_onboarding SET password_hash = NULL,
                       updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1"""
                )
            _audit(connection, user_id, user_id, "first_admin")
            return Account(
                user_id, normalized, AccountRole.ADMIN, AccountStatus.ACTIVE, frozenset()
            )

    def authenticate(self, email: str, password: str) -> Account | None:
        try:
            normalized = _email(email)
        except ValueError:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, role, status, grants, password_hash FROM review_users WHERE email = %s",
                (normalized,),
            ).fetchone()
        if row is None or row[3] != "active" or not verify_owner_password(password, str(row[5])):
            return None
        return _account(row[:5])

    def create_session(self, user_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as connection, connection.transaction():
            changed = connection.execute(
                """INSERT INTO review_sessions (token_hash, user_id, expires_at)
                   SELECT %s, id, CURRENT_TIMESTAMP + INTERVAL '30 days'
                   FROM review_users WHERE id = %s AND status = 'active' FOR SHARE""",
                (_digest(token), user_id),
            ).rowcount
            if changed != 1:
                raise ValueError("active account required")
        return token

    def load_principal(self, token: str) -> Account | None:
        if not token or len(token) > 256:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """SELECT u.id, u.email, u.role, u.status, u.grants
                   FROM review_sessions s JOIN review_users u ON u.id = s.user_id
                   WHERE s.token_hash = %s AND s.expires_at > CURRENT_TIMESTAMP
                     AND s.revoked_at IS NULL AND u.status = 'active'""",
                (_digest(token),),
            ).fetchone()
        return _account(row) if row else None

    def revoke_session(self, token: str) -> None:
        with self.connect() as connection, connection.transaction():
            _ = connection.execute(
                "UPDATE review_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE token_hash = %s AND revoked_at IS NULL",
                (_digest(token),),
            )

    def revoke_user_sessions(self, user_id: UUID) -> None:
        with self.connect() as connection, connection.transaction():
            _ = connection.execute(
                "UPDATE review_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )

    def list_members(self) -> tuple[Account, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, email, role, status, grants FROM review_users ORDER BY email"
            ).fetchall()
        return tuple(_account(row) for row in rows)

    def set_member(
        self,
        actor_id: UUID,
        subject_id: UUID,
        *,
        role: AccountRole,
        grants: frozenset[Capability],
        status: AccountStatus,
    ) -> Account:
        _validate_grants(role, grants)
        with self.connect() as connection, connection.transaction():
            _lock_membership(connection)
            actor = _require_member_manager(connection, actor_id)
            old = _load_account(connection, subject_id)
            if old is None:
                raise ValueError("member not found")
            if (
                old.role is AccountRole.ADMIN or role is AccountRole.ADMIN
            ) and actor.role is not AccountRole.ADMIN:
                raise PermissionError("admin role changes require an admin")
            _ = connection.execute(
                """UPDATE review_users SET role = %s, grants = %s, status = %s,
                   updated_at = CURRENT_TIMESTAMP WHERE id = %s""",
                (role.value, _stored_grants(grants), status.value, subject_id),
            )
            if old.role != role:
                _audit(connection, actor_id, subject_id, "role_changed")
            if old.grants != grants:
                _audit(connection, actor_id, subject_id, "grants_changed")
            if old.status != status:
                _audit(
                    connection,
                    actor_id,
                    subject_id,
                    "enabled" if status is AccountStatus.ACTIVE else "disabled",
                )
                if status is AccountStatus.DISABLED:
                    _ = connection.execute(
                        """UPDATE review_sessions SET revoked_at = CURRENT_TIMESTAMP
                           WHERE user_id = %s AND revoked_at IS NULL""",
                        (subject_id,),
                    )
            return Account(subject_id, old.email, role, status, grants)

    def issue_invite(
        self, actor_id: UUID, email: str, *, role: AccountRole, grants: frozenset[Capability]
    ) -> str:
        _validate_grants(role, grants)
        normalized = _email(email)
        token = secrets.token_urlsafe(32)
        with self.connect() as connection, connection.transaction():
            actor = _require_member_manager(connection, actor_id)
            if role is AccountRole.ADMIN and actor.role is not AccountRole.ADMIN:
                raise PermissionError("admin invitations require an admin")
            if connection.execute(
                "SELECT 1 FROM review_users WHERE email = %s", (normalized,)
            ).fetchone():
                raise ValueError("account already exists")
            _ = connection.execute(
                """INSERT INTO review_account_tokens
                   (token_hash, purpose, email, role, grants, created_by, expires_at)
                   VALUES (%s, 'invite', %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '7 days')""",
                (_digest(token), normalized, role.value, _stored_grants(grants), actor_id),
            )
            _audit(
                connection,
                actor_id,
                None,
                "invited",
                {
                    "email": normalized,
                    "role": role.value,
                    "grants": _stored_grants(grants),
                },
            )
        return token

    def accept_invite(self, token: str, password: str) -> Account | None:
        encoded = hash_owner_password(password)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE review_account_tokens SET consumed_at = CURRENT_TIMESTAMP
                   WHERE token_hash = %s AND purpose = 'invite' AND consumed_at IS NULL
                     AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                   RETURNING email, role, grants, created_by""",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            email, role, grants, inviter = (
                str(row[0]),
                AccountRole(str(row[1])),
                _grants(row[2]),
                UUID(str(row[3])),
            )
            inviter_account = _load_account(connection, inviter, lock=True)
            if inviter_account is None or not _can_manage_members(inviter_account):
                return None
            if role is AccountRole.ADMIN and inviter_account.role is not AccountRole.ADMIN:
                return None
            user_id = uuid4()
            _ = connection.execute(
                """INSERT INTO review_users (id, email, password_hash, role, grants)
                   VALUES (%s, %s, %s, %s, %s)""",
                (user_id, email, encoded, role.value, _stored_grants(grants)),
            )
            _audit(connection, inviter, user_id, "created")
            return Account(user_id, email, role, AccountStatus.ACTIVE, grants)

    def issue_reset(self, actor_id: UUID, subject_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as connection, connection.transaction():
            actor = _require_member_manager(connection, actor_id)
            subject = _load_account(connection, subject_id)
            if subject is None:
                raise ValueError("member not found")
            if subject.status is not AccountStatus.ACTIVE:
                raise ValueError("active member required")
            if subject.role is AccountRole.ADMIN and actor.role is not AccountRole.ADMIN:
                raise PermissionError("admin password reset requires an admin")
            _ = connection.execute(
                """UPDATE review_account_tokens SET revoked_at = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND purpose = 'password_reset' AND consumed_at IS NULL
                     AND revoked_at IS NULL""",
                (subject_id,),
            )
            _ = connection.execute(
                """INSERT INTO review_account_tokens
                   (token_hash, purpose, user_id, created_by, expires_at)
                   VALUES (%s, 'password_reset', %s, %s, CURRENT_TIMESTAMP + INTERVAL '1 hour')""",
                (_digest(token), subject_id, actor_id),
            )
            _audit(connection, actor_id, subject_id, "password_reset_issued")
        return token

    def accept_reset(self, token: str, password: str) -> bool:
        encoded = hash_owner_password(password)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE review_account_tokens SET consumed_at = CURRENT_TIMESTAMP
                   WHERE token_hash = %s AND purpose = 'password_reset' AND consumed_at IS NULL
                     AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                   RETURNING user_id, created_by""",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return False
            user_id, issuer_id = UUID(str(row[0])), UUID(str(row[1]))
            issuer = _load_account(connection, issuer_id, lock=True)
            subject = _load_account(connection, user_id, lock=True)
            if issuer is None or not _can_manage_members(issuer):
                return False
            if subject is None or subject.status is not AccountStatus.ACTIVE:
                return False
            if subject.role is AccountRole.ADMIN and issuer.role is not AccountRole.ADMIN:
                return False
            _ = connection.execute(
                "UPDATE review_users SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (encoded, user_id),
            )
            _ = connection.execute(
                "UPDATE review_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
            _audit(connection, user_id, user_id, "password_reset")
            return True

    def revoke_link(self, token: str) -> None:
        with self.connect() as connection, connection.transaction():
            _ = connection.execute(
                "UPDATE review_account_tokens SET revoked_at = CURRENT_TIMESTAMP WHERE token_hash = %s AND consumed_at IS NULL AND revoked_at IS NULL",
                (_digest(token),),
            )


def _email(value: str) -> str:
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 320
        or "@" not in normalized
        or any(c.isspace() for c in normalized)
    ):
        raise ValueError("invalid email address")
    return normalized


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _grants(value: object) -> frozenset[Capability]:
    if not isinstance(value, list):
        raise ValueError("invalid stored grants")
    return frozenset(Capability(str(item)) for item in cast(list[object], value))


def _stored_grants(grants: frozenset[Capability]) -> list[str]:
    return sorted(capability.value for capability in grants)


def _validate_grants(role: AccountRole, grants: frozenset[Capability]) -> None:
    if role is AccountRole.ADMIN and grants:
        raise ValueError("admin grants must be empty")
    for grant in grants:
        area, _, action = grant.value.partition(".")
        if action != "view" and area in {"review", "search", "activity", "control", "admin"}:
            if Capability(f"{area}.view") not in grants:
                raise ValueError("actions require area view access")


def _account(row: tuple[object, ...]) -> Account:
    return Account(
        UUID(str(row[0])),
        str(row[1]),
        AccountRole(str(row[2])),
        AccountStatus(str(row[3])),
        _grants(row[4]),
    )


def _load_account(connection: Connection, user_id: UUID, *, lock: bool = False) -> Account | None:
    row = connection.execute(
        "SELECT id, email, role, status, grants FROM review_users WHERE id = %s"
        + (" FOR UPDATE" if lock else ""),
        (user_id,),
    ).fetchone()
    return _account(row) if row else None


def _lock_membership(connection: Connection) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        is None
    ):
        raise RuntimeError("owner onboarding state is missing")


def _require_member_manager(
    connection: Connection, actor_id: UUID, *, lock: bool = False
) -> Account:
    actor = _load_account(connection, actor_id, lock=lock)
    if not _can_manage_members(actor):
        raise PermissionError("member management access required")
    assert actor is not None
    return actor


def _can_manage_members(account: Account | None) -> bool:
    return (
        account is not None
        and account.status is AccountStatus.ACTIVE
        and has_capability(account.capabilities, Capability.ADMIN_MEMBERS)
    )


def _audit(
    connection: Connection,
    actor_id: UUID,
    subject_id: UUID | None,
    action: str,
    detail: dict[str, object] | None = None,
) -> None:
    _ = connection.execute(
        """INSERT INTO review_membership_audit (id, actor_id, subject_id, action, detail)
           VALUES (%s, %s, %s, %s, %s)""",
        (uuid4(), actor_id, subject_id, action, Jsonb(detail or {})),
    )
