from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from job_finder.qualification_definition import (
    QualificationDefinition,
    QualificationDefinitionRevisionId,
    qualification_definition_revision_id,
)

_Connection = psycopg.Connection[tuple[object, ...]]
_RevisionId = Annotated[QualificationDefinitionRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]


class QualificationDefinitionServiceError(ValueError):
    pass


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class QualificationDefinitionRevision(_Model):
    id: _RevisionId
    definition: QualificationDefinition
    created_at: datetime
    created_by: str


class QualificationDefinitionDraft(_Model):
    base_revision_id: _RevisionId
    version: int = Field(ge=0)
    definition: QualificationDefinition
    updated_at: datetime
    updated_by: str


class ReplaceQualificationDefinitionDraftCommand(_Model):
    expected_base_revision_id: _RevisionId
    expected_version: int = Field(ge=0, le=2**63 - 2)
    definition: QualificationDefinition
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class QualificationDraftSaved(_Model):
    kind: Literal["saved"] = "saved"
    draft: QualificationDefinitionDraft


class QualificationDraftChanged(_Model):
    kind: Literal["draft_changed"] = "draft_changed"
    current_draft: QualificationDefinitionDraft


class PublishQualificationDefinitionCommand(_Model):
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_draft_version: int = Field(ge=0, le=2**63 - 2)
    expected_revision_id: _RevisionId
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class QualificationPublicationReceipt(_Model):
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


def load_qualification_definition_revision(
    connection: _Connection, revision_id: QualificationDefinitionRevisionId
) -> QualificationDefinitionRevision:
    row = connection.execute(
        "SELECT id, content, created_at, created_by FROM qualification_definition_revisions WHERE id = %s",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise QualificationDefinitionServiceError(
            "Qualification definition revision does not exist"
        )
    definition = QualificationDefinition.model_validate(row[1])
    if qualification_definition_revision_id(definition) != row[0]:
        raise QualificationDefinitionServiceError(
            "Qualification definition revision identity differs from content"
        )
    return QualificationDefinitionRevision.model_validate(
        {"id": row[0], "definition": definition, "created_at": row[2], "created_by": row[3]}
    )


def get_qualification_definition_draft(connection: _Connection) -> QualificationDefinitionDraft:
    row = connection.execute(
        "SELECT base_revision_id, version, content, updated_at, updated_by FROM qualification_definition_drafts WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise QualificationDefinitionServiceError("Qualification definition draft is missing")
    base_revision_id = QualificationDefinitionRevisionId(str(row[0]))
    _ = load_qualification_definition_revision(connection, base_revision_id)
    return _draft_from_row(row)


def replace_qualification_definition_draft(
    connection: _Connection, command: ReplaceQualificationDefinitionDraftCommand
) -> QualificationDraftSaved | QualificationDraftChanged:
    if not connection.autocommit:
        raise QualificationDefinitionServiceError("Qualification draft edits require autocommit")
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE qualification_definition_drafts
            SET version = version + 1, content = %s, updated_at = %s, updated_by = %s
            WHERE singleton_id = 1 AND base_revision_id = %s AND version = %s
            RETURNING base_revision_id, version, content, updated_at, updated_by
            """,
            (
                Jsonb(command.definition.model_dump(mode="json")),
                command.timestamp,
                command.actor,
                command.expected_base_revision_id,
                command.expected_version,
            ),
        ).fetchone()
        if row is not None:
            return QualificationDraftSaved(draft=_draft_from_row(row))
        return QualificationDraftChanged(
            current_draft=get_qualification_definition_draft(connection)
        )


def _draft_from_row(row: tuple[object, ...]) -> QualificationDefinitionDraft:
    return QualificationDefinitionDraft.model_validate(
        {
            "base_revision_id": row[0],
            "version": row[1],
            "definition": row[2],
            "updated_at": row[3],
            "updated_by": row[4],
        }
    )


def publish_qualification_definition(
    connection: _Connection, command: PublishQualificationDefinitionCommand
) -> QualificationPublicationReceipt:
    if not connection.autocommit:
        raise QualificationDefinitionServiceError("Qualification publication requires autocommit")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"qualification_definition_publication:{command.idempotency_key}",),
        )
        existing = load_qualification_definition_publication_receipt(
            connection, command.idempotency_key
        )
        if existing is not None:
            _require_matching_publication_request(existing, command)
            return existing.model_copy(update={"replayed": True})
        row = connection.execute(
            "SELECT base_revision_id, version, content, updated_at, updated_by FROM qualification_definition_drafts WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise QualificationDefinitionServiceError("Qualification definition draft is missing")
        draft = _draft_from_row(row)
        observed_id = qualification_definition_revision_id(draft.definition)
        if (
            draft.version != command.expected_draft_version
            or observed_id != command.expected_revision_id
        ):
            _store_draft_changed_receipt(connection, command, draft.version, observed_id)
        else:
            _publish_locked_draft(connection, command, draft)
        receipt = load_qualification_definition_publication_receipt(
            connection, command.idempotency_key
        )
        if receipt is None:
            raise QualificationDefinitionServiceError(
                "Qualification publication receipt is missing"
            )
        return receipt


def _publish_locked_draft(
    connection: _Connection,
    command: PublishQualificationDefinitionCommand,
    draft: QualificationDefinitionDraft,
) -> None:
    revision_id = qualification_definition_revision_id(draft.definition)
    _ = connection.execute(
        """
        INSERT INTO qualification_definition_revisions (id, content, created_at, created_by)
        VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
        """,
        (
            revision_id,
            Jsonb(draft.definition.model_dump(mode="json")),
            command.timestamp,
            command.actor,
        ),
    )
    stored = load_qualification_definition_revision(connection, revision_id)
    if stored.definition != draft.definition:
        raise QualificationDefinitionServiceError(
            "Stored qualification revision differs from draft"
        )
    _ = connection.execute(
        """
        INSERT INTO qualification_definition_publications (revision_id, published_at, published_by)
        VALUES (%s, %s, %s) ON CONFLICT (revision_id) DO NOTHING
        """,
        (revision_id, command.timestamp, command.actor),
    )
    changed = connection.execute(
        """
        UPDATE qualification_definition_drafts
        SET base_revision_id = %s, version = version + 1,
            content = %s, updated_at = %s, updated_by = %s
        WHERE singleton_id = 1 AND base_revision_id = %s AND version = %s
        """,
        (
            revision_id,
            Jsonb(draft.definition.model_dump(mode="json")),
            command.timestamp,
            command.actor,
            draft.base_revision_id,
            draft.version,
        ),
    ).rowcount
    if changed != 1:
        raise QualificationDefinitionServiceError("Locked qualification draft changed unexpectedly")
    _ = connection.execute(
        """
        INSERT INTO qualification_definition_publication_receipts (
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
    command: PublishQualificationDefinitionCommand,
    observed_version: int,
    observed_id: QualificationDefinitionRevisionId,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO qualification_definition_publication_receipts (
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


def load_qualification_definition_publication_receipt(
    connection: _Connection, idempotency_key: str
) -> QualificationPublicationReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, outcome, expected_draft_version,
               expected_revision_id, actor, requested_at,
               observed_draft_version, observed_revision_id,
               publication_revision_id, rebased_draft_version
        FROM qualification_definition_publication_receipts WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return QualificationPublicationReceipt.model_validate(
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
    receipt: QualificationPublicationReceipt, command: PublishQualificationDefinitionCommand
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
        raise QualificationDefinitionServiceError(
            "Idempotency key belongs to another qualification publication"
        )
