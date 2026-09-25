from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.qualification_definition import (
    PersonalCriterion,
    qualification_definition_revision_id,
)
from job_finder.qualification_definition_service import (
    PublishQualificationDefinitionCommand,
    QualificationDraftChanged,
    QualificationDraftSaved,
    ReplaceQualificationDefinitionDraftCommand,
    get_qualification_definition_draft,
    load_qualification_definition_revision,
    publish_qualification_definition,
    replace_qualification_definition_draft,
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    name = f"job_finder_qualification_draft_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            _ = connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def test_qualification_draft_publication_is_independent_and_idempotent(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
        )
        _ = apply_migrations(connection)
        draft = get_qualification_definition_draft(connection)
        before_acquisition = connection.execute(
            "SELECT base_revision_id, version, content FROM acquisition_policy_drafts WHERE singleton_id = 1"
        ).fetchone()
        before_legacy = connection.execute(
            "SELECT base_revision_id, version, content FROM search_configuration_drafts WHERE singleton_id = 1"
        ).fetchone()
        updated = draft.definition.model_copy(
            update={
                "personal_criteria": (
                    *draft.definition.personal_criteria,
                    PersonalCriterion(
                        key="onsite-preference",
                        name="Onsite preference",
                        instructions="Prefer roles with a clear remote option.",
                    ),
                )
            }
        )
        command = ReplaceQualificationDefinitionDraftCommand(
            expected_base_revision_id=draft.base_revision_id,
            expected_version=draft.version,
            definition=updated,
            actor="owner",
            timestamp=now,
        )
        saved = replace_qualification_definition_draft(connection, command)
        assert isinstance(saved, QualificationDraftSaved)
        assert saved.draft.version == draft.version + 1
        assert saved.draft.definition == updated
        stale = replace_qualification_definition_draft(connection, command)
        assert isinstance(stale, QualificationDraftChanged)
        assert stale.current_draft == saved.draft
        revision_id = qualification_definition_revision_id(updated)
        publish = PublishQualificationDefinitionCommand(
            idempotency_key="publish-qualification-definition",
            expected_draft_version=saved.draft.version,
            expected_revision_id=revision_id,
            actor="owner",
            timestamp=now,
        )
        publication = publish_qualification_definition(connection, publish)
        assert publication.outcome == "published"
        assert publication.publication_revision_id == revision_id
        assert load_qualification_definition_revision(connection, revision_id).definition == updated
        assert publish_qualification_definition(connection, publish).replayed
        with pytest.raises(ValueError, match="another qualification publication"):
            _ = publish_qualification_definition(
                connection, publish.model_copy(update={"actor": "different"})
            )
        changed = publish_qualification_definition(
            connection,
            publish.model_copy(update={"idempotency_key": "stale-qualification-definition"}),
        )
        assert changed.outcome == "draft_changed"
        assert changed.observed_draft_version == saved.draft.version + 1
        assert get_qualification_definition_draft(connection).base_revision_id == revision_id
        assert (
            connection.execute(
                "SELECT base_revision_id, version, content FROM acquisition_policy_drafts WHERE singleton_id = 1"
            ).fetchone()
            == before_acquisition
        )
        assert (
            connection.execute(
                "SELECT base_revision_id, version, content FROM search_configuration_drafts WHERE singleton_id = 1"
            ).fetchone()
            == before_legacy
        )
