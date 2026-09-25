from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from job_finder.acquisition_policy import (
    AcquisitionPolicy,
    AcquisitionPolicyRevisionId,
    acquisition_policy_revision_id,
)

_Connection = psycopg.Connection[tuple[object, ...]]
_RevisionId = Annotated[AcquisitionPolicyRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]


class AcquisitionPolicyServiceError(ValueError):
    pass


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class AcquisitionPolicyRevision(_Model):
    id: _RevisionId
    policy: AcquisitionPolicy
    created_at: datetime
    created_by: str


class ActiveAcquisitionPolicy(_Model):
    revision: AcquisitionPolicyRevision
    generation: int = Field(ge=0)
    activated_at: datetime
    activated_by: str


class AcquisitionPolicyDraft(_Model):
    base_revision_id: _RevisionId
    version: int = Field(ge=0)
    policy: AcquisitionPolicy
    updated_at: datetime
    updated_by: str


class ReplaceAcquisitionPolicyDraftCommand(_Model):
    expected_base_revision_id: _RevisionId
    expected_version: int = Field(ge=0, le=2**63 - 2)
    policy: AcquisitionPolicy
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class AcquisitionDraftSaved(_Model):
    kind: Literal["saved"] = "saved"
    draft: AcquisitionPolicyDraft


class AcquisitionDraftChanged(_Model):
    kind: Literal["draft_changed"] = "draft_changed"
    current_draft: AcquisitionPolicyDraft


class PublishAcquisitionPolicyCommand(_Model):
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_draft_version: int = Field(ge=0, le=2**63 - 2)
    expected_revision_id: _RevisionId
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class AcquisitionPublicationReceipt(_Model):
    idempotency_key: str
    outcome: Literal["published", "draft_changed"]
    expected_draft_version: int
    expected_revision_id: _RevisionId
    actor: str
    requested_at: datetime
    observed_draft_version: int | None
    observed_revision_id: _RevisionId | None
    publication_revision_id: _RevisionId | None
    rebased_draft_version: int | None
    replayed: bool = False


def load_acquisition_policy_revision(
    connection: _Connection, revision_id: AcquisitionPolicyRevisionId
) -> AcquisitionPolicyRevision:
    row = connection.execute(
        "SELECT id, content, created_at, created_by FROM acquisition_policy_revisions WHERE id = %s",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise AcquisitionPolicyServiceError("Acquisition policy revision does not exist")
    policy = AcquisitionPolicy.model_validate(row[1])
    if acquisition_policy_revision_id(policy) != row[0]:
        raise AcquisitionPolicyServiceError(
            "Acquisition policy revision identity differs from content"
        )
    return AcquisitionPolicyRevision.model_validate(
        {"id": row[0], "policy": policy, "created_at": row[2], "created_by": row[3]}
    )


def get_active_acquisition_policy(connection: _Connection) -> ActiveAcquisitionPolicy:
    row = connection.execute(
        "SELECT revision_id, generation, activated_at, activated_by FROM active_acquisition_policy WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise AcquisitionPolicyServiceError("Active acquisition policy is missing")
    return ActiveAcquisitionPolicy.model_validate(
        {
            "revision": load_acquisition_policy_revision(
                connection, AcquisitionPolicyRevisionId(str(row[0]))
            ),
            "generation": row[1],
            "activated_at": row[2],
            "activated_by": row[3],
        }
    )


def get_acquisition_policy_draft(connection: _Connection) -> AcquisitionPolicyDraft:
    row = connection.execute(
        "SELECT base_revision_id, version, content, updated_at, updated_by FROM acquisition_policy_drafts WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise AcquisitionPolicyServiceError("Acquisition policy draft is missing")
    base_revision_id = AcquisitionPolicyRevisionId(str(row[0]))
    _ = load_acquisition_policy_revision(connection, base_revision_id)
    return _draft_from_row(row)


def replace_acquisition_policy_draft(
    connection: _Connection, command: ReplaceAcquisitionPolicyDraftCommand
) -> AcquisitionDraftSaved | AcquisitionDraftChanged:
    if not connection.autocommit:
        raise AcquisitionPolicyServiceError("Acquisition draft edits require autocommit")
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE acquisition_policy_drafts
            SET version = version + 1, content = %s, updated_at = %s, updated_by = %s
            WHERE singleton_id = 1 AND base_revision_id = %s AND version = %s
            RETURNING base_revision_id, version, content, updated_at, updated_by
            """,
            (
                Jsonb(command.policy.model_dump(mode="json")),
                command.timestamp,
                command.actor,
                command.expected_base_revision_id,
                command.expected_version,
            ),
        ).fetchone()
        if row is not None:
            return AcquisitionDraftSaved(draft=_draft_from_row(row))
        return AcquisitionDraftChanged(current_draft=get_acquisition_policy_draft(connection))


def _draft_from_row(row: tuple[object, ...]) -> AcquisitionPolicyDraft:
    return AcquisitionPolicyDraft.model_validate(
        {
            "base_revision_id": row[0],
            "version": row[1],
            "policy": row[2],
            "updated_at": row[3],
            "updated_by": row[4],
        }
    )


def publish_acquisition_policy(
    connection: _Connection, command: PublishAcquisitionPolicyCommand
) -> AcquisitionPublicationReceipt:
    if not connection.autocommit:
        raise AcquisitionPolicyServiceError("Acquisition publication requires autocommit")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"acquisition_publication:{command.idempotency_key}",),
        )
        existing = load_acquisition_publication_receipt(connection, command.idempotency_key)
        if existing is not None:
            _require_matching_publication_request(existing, command)
            return existing.model_copy(update={"replayed": True})
        row = connection.execute(
            "SELECT base_revision_id, version, content, updated_at, updated_by FROM acquisition_policy_drafts WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise AcquisitionPolicyServiceError("Acquisition policy draft is missing")
        draft = _draft_from_row(row)
        observed_id = acquisition_policy_revision_id(draft.policy)
        if (
            draft.version != command.expected_draft_version
            or observed_id != command.expected_revision_id
        ):
            _store_draft_changed_receipt(connection, command, draft.version, observed_id)
        else:
            _publish_locked_draft(connection, command, draft)
        receipt = load_acquisition_publication_receipt(connection, command.idempotency_key)
        if receipt is None:
            raise AcquisitionPolicyServiceError("Acquisition publication receipt is missing")
        return receipt


def _publish_locked_draft(
    connection: _Connection,
    command: PublishAcquisitionPolicyCommand,
    draft: AcquisitionPolicyDraft,
) -> None:
    revision_id = acquisition_policy_revision_id(draft.policy)
    _ = connection.execute(
        """
        INSERT INTO acquisition_policy_revisions (id, content, created_at, created_by)
        VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
        """,
        (
            revision_id,
            Jsonb(draft.policy.model_dump(mode="json")),
            command.timestamp,
            command.actor,
        ),
    )
    stored = load_acquisition_policy_revision(connection, revision_id)
    if stored.policy != draft.policy:
        raise AcquisitionPolicyServiceError("Stored acquisition revision differs from draft")
    _ = connection.execute(
        """
        INSERT INTO acquisition_policy_publications (revision_id, published_at, published_by)
        VALUES (%s, %s, %s) ON CONFLICT (revision_id) DO NOTHING
        """,
        (revision_id, command.timestamp, command.actor),
    )
    changed = connection.execute(
        """
        UPDATE acquisition_policy_drafts
        SET base_revision_id = %s, version = version + 1,
            content = %s, updated_at = %s, updated_by = %s
        WHERE singleton_id = 1 AND base_revision_id = %s AND version = %s
        """,
        (
            revision_id,
            Jsonb(draft.policy.model_dump(mode="json")),
            command.timestamp,
            command.actor,
            draft.base_revision_id,
            draft.version,
        ),
    ).rowcount
    if changed != 1:
        raise AcquisitionPolicyServiceError("Locked acquisition draft changed unexpectedly")
    _ = connection.execute(
        """
        INSERT INTO acquisition_policy_publication_receipts (
          idempotency_key, outcome, expected_draft_version,
          expected_revision_id, actor, requested_at,
          publication_revision_id, rebased_draft_version
        ) VALUES (%s, 'published', %s, %s, %s, %s, %s, %s)
        """,
        (
            command.idempotency_key,
            command.expected_draft_version,
            command.expected_revision_id,
            command.actor,
            command.timestamp,
            revision_id,
            draft.version + 1,
        ),
    )


def _store_draft_changed_receipt(
    connection: _Connection,
    command: PublishAcquisitionPolicyCommand,
    observed_version: int,
    observed_id: AcquisitionPolicyRevisionId,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO acquisition_policy_publication_receipts (
          idempotency_key, outcome, expected_draft_version,
          expected_revision_id, actor, requested_at,
          observed_draft_version, observed_revision_id
        ) VALUES (%s, 'draft_changed', %s, %s, %s, %s, %s, %s)
        """,
        (
            command.idempotency_key,
            command.expected_draft_version,
            command.expected_revision_id,
            command.actor,
            command.timestamp,
            observed_version,
            observed_id,
        ),
    )


def load_acquisition_publication_receipt(
    connection: _Connection, idempotency_key: str
) -> AcquisitionPublicationReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, outcome, expected_draft_version,
               expected_revision_id, actor, requested_at,
               observed_draft_version, observed_revision_id,
               publication_revision_id, rebased_draft_version
        FROM acquisition_policy_publication_receipts WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return AcquisitionPublicationReceipt.model_validate(
        dict(
            zip(
                (
                    "idempotency_key",
                    "outcome",
                    "expected_draft_version",
                    "expected_revision_id",
                    "actor",
                    "requested_at",
                    "observed_draft_version",
                    "observed_revision_id",
                    "publication_revision_id",
                    "rebased_draft_version",
                ),
                row,
                strict=True,
            )
        )
    )


def _require_matching_publication_request(
    receipt: AcquisitionPublicationReceipt, command: PublishAcquisitionPolicyCommand
) -> None:
    if (
        receipt.expected_draft_version,
        receipt.expected_revision_id,
        receipt.actor,
    ) != (
        command.expected_draft_version,
        command.expected_revision_id,
        command.actor,
    ):
        raise AcquisitionPolicyServiceError(
            "Idempotency key belongs to another acquisition publication"
        )
