CREATE TABLE search_configuration_publication_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  outcome TEXT NOT NULL CHECK (outcome IN ('published', 'draft_changed')),
  expected_draft_version BIGINT NOT NULL CHECK (expected_draft_version >= 0),
  expected_configuration_revision_id CHAR(64) NOT NULL
    CHECK (expected_configuration_revision_id ~ '^[0-9a-f]{64}$'),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  observed_draft_version BIGINT CHECK (observed_draft_version >= 0),
  observed_configuration_revision_id CHAR(64)
    CHECK (observed_configuration_revision_id ~ '^[0-9a-f]{64}$'),
  publication_revision_id CHAR(64)
    REFERENCES search_configuration_publications(revision_id),
  rebased_draft_version BIGINT CHECK (rebased_draft_version >= 1),
  CONSTRAINT search_configuration_publication_receipt_outcome_shape CHECK (
    (
      outcome = 'published'
      AND publication_revision_id IS NOT NULL
      AND rebased_draft_version IS NOT NULL
      AND publication_revision_id = expected_configuration_revision_id
      AND rebased_draft_version = expected_draft_version + 1
      AND observed_draft_version IS NULL
      AND observed_configuration_revision_id IS NULL
    ) OR (
      outcome = 'draft_changed'
      AND publication_revision_id IS NULL
      AND rebased_draft_version IS NULL
      AND observed_draft_version IS NOT NULL
      AND observed_configuration_revision_id IS NOT NULL
      AND (
        observed_draft_version <> expected_draft_version
        OR observed_configuration_revision_id <> expected_configuration_revision_id
      )
    )
  )
);

CREATE TRIGGER search_configuration_publication_receipts_are_immutable
BEFORE UPDATE OR DELETE ON search_configuration_publication_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
