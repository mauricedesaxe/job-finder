from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.acquisition_policy import acquisition_policy_revision_id
from job_finder.acquisition_policy_service import (
    AcquisitionDraftChanged,
    AcquisitionDraftSaved,
    PublishAcquisitionPolicyCommand,
    ReplaceAcquisitionPolicyDraftCommand,
    get_acquisition_policy_draft,
    get_active_acquisition_policy,
    publish_acquisition_policy,
    replace_acquisition_policy_draft,
)
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_acquisition_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_acquisition_draft_edits_are_independent_and_optimistic(authority_schema: str) -> None:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(authority_schema))
        )
        _ = apply_migrations(connection)
        active = get_active_acquisition_policy(connection)
        draft = get_acquisition_policy_draft(connection)
        assert active.revision.id == draft.base_revision_id
        before_qualification = connection.execute(
            "SELECT base_revision_id, version, content FROM qualification_definition_drafts WHERE singleton_id = 1"
        ).fetchone()
        before_legacy = connection.execute(
            "SELECT base_revision_id, version, content FROM search_configuration_drafts WHERE singleton_id = 1"
        ).fetchone()
        updated = draft.policy.model_copy(
            update={"search_keywords": (*draft.policy.search_keywords, "site reliability engineer")}
        )
        command = ReplaceAcquisitionPolicyDraftCommand(
            expected_base_revision_id=draft.base_revision_id,
            expected_version=draft.version,
            policy=updated,
            actor="owner",
            timestamp=datetime(2026, 9, 25, tzinfo=UTC),
        )
        saved = replace_acquisition_policy_draft(connection, command)
        assert isinstance(saved, AcquisitionDraftSaved)
        assert saved.draft.version == draft.version + 1
        assert saved.draft.policy == updated
        stale = replace_acquisition_policy_draft(connection, command)
        assert isinstance(stale, AcquisitionDraftChanged)
        assert stale.current_draft == saved.draft
        assert get_active_acquisition_policy(connection) == active
        assert (
            connection.execute(
                "SELECT base_revision_id, version, content FROM qualification_definition_drafts WHERE singleton_id = 1"
            ).fetchone()
            == before_qualification
        )
        assert (
            connection.execute(
                "SELECT base_revision_id, version, content FROM search_configuration_drafts WHERE singleton_id = 1"
            ).fetchone()
            == before_legacy
        )

        publish = PublishAcquisitionPolicyCommand(
            idempotency_key="publish-acquisition-draft",
            expected_draft_version=saved.draft.version,
            expected_revision_id=acquisition_policy_revision_id(updated),
            actor="owner",
            timestamp=datetime(2026, 9, 25, tzinfo=UTC),
        )
        publication = publish_acquisition_policy(connection, publish)
        assert publication.outcome == "published"
        assert publication.publication_revision_id == publish.expected_revision_id
        assert publication.rebased_draft_version == saved.draft.version + 1
        assert publish_acquisition_policy(connection, publish).replayed
        with pytest.raises(ValueError, match="another acquisition publication"):
            _ = publish_acquisition_policy(
                connection, publish.model_copy(update={"actor": "different"})
            )
        stale_command = publish.model_copy(
            update={"idempotency_key": "stale-acquisition-publication"}
        )
        stale_publication = publish_acquisition_policy(connection, stale_command)
        assert stale_publication.outcome == "draft_changed"
        assert stale_publication.observed_draft_version == saved.draft.version + 1
        assert publish_acquisition_policy(connection, stale_command).replayed
        assert get_active_acquisition_policy(connection) == active
        assert (
            connection.execute(
                "SELECT base_revision_id, version, content FROM qualification_definition_drafts WHERE singleton_id = 1"
            ).fetchone()
            == before_qualification
        )
        assert (
            connection.execute(
                "SELECT base_revision_id, version, content FROM search_configuration_drafts WHERE singleton_id = 1"
            ).fetchone()
            == before_legacy
        )
