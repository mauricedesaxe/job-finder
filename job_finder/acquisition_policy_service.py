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
