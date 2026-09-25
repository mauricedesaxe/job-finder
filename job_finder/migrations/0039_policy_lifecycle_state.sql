SELECT backfill_legacy_search_configuration_policies();

ALTER TABLE legacy_search_configuration_policy_projections
ADD CONSTRAINT legacy_policy_projection_identity UNIQUE (
  legacy_revision_id, acquisition_policy_revision_id,
  qualification_definition_revision_id
);

CREATE TABLE acquisition_policy_publications (
  revision_id CHAR(64) PRIMARY KEY REFERENCES acquisition_policy_revisions(id),
  published_at TIMESTAMPTZ NOT NULL,
  published_by TEXT NOT NULL CHECK (published_by <> '')
);
CREATE TRIGGER acquisition_policy_publications_are_immutable
BEFORE UPDATE OR DELETE ON acquisition_policy_publications
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE qualification_definition_publications (
  revision_id CHAR(64) PRIMARY KEY REFERENCES qualification_definition_revisions(id),
  published_at TIMESTAMPTZ NOT NULL,
  published_by TEXT NOT NULL CHECK (published_by <> '')
);
CREATE TRIGGER qualification_definition_publications_are_immutable
BEFORE UPDATE OR DELETE ON qualification_definition_publications
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE legacy_search_configuration_publication_projections (
  legacy_revision_id CHAR(64) PRIMARY KEY
    REFERENCES search_configuration_publications(revision_id),
  acquisition_policy_revision_id CHAR(64) NOT NULL
    REFERENCES acquisition_policy_publications(revision_id),
  qualification_definition_revision_id CHAR(64) NOT NULL
    REFERENCES qualification_definition_publications(revision_id),
  FOREIGN KEY (
    legacy_revision_id, acquisition_policy_revision_id,
    qualification_definition_revision_id
  ) REFERENCES legacy_search_configuration_policy_projections (
    legacy_revision_id, acquisition_policy_revision_id,
    qualification_definition_revision_id
  )
);
CREATE TRIGGER legacy_search_configuration_publication_projections_are_immutable
BEFORE UPDATE OR DELETE ON legacy_search_configuration_publication_projections
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION backfill_legacy_search_configuration_publications()
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  legacy RECORD;
  stored_acquisition_id CHAR(64);
  stored_qualification_id CHAR(64);
BEGIN
  PERFORM backfill_legacy_search_configuration_policies();
  FOR legacy IN
    SELECT publication.revision_id, publication.published_at, publication.published_by,
           projection.acquisition_policy_revision_id,
           projection.qualification_definition_revision_id
    FROM search_configuration_publications publication
    JOIN legacy_search_configuration_policy_projections projection
      ON projection.legacy_revision_id = publication.revision_id
    ORDER BY publication.published_at, publication.revision_id
  LOOP
    INSERT INTO acquisition_policy_publications (revision_id, published_at, published_by)
    VALUES (
      legacy.acquisition_policy_revision_id, legacy.published_at, legacy.published_by
    ) ON CONFLICT (revision_id) DO NOTHING;
    INSERT INTO qualification_definition_publications (
      revision_id, published_at, published_by
    ) VALUES (
      legacy.qualification_definition_revision_id, legacy.published_at,
      legacy.published_by
    ) ON CONFLICT (revision_id) DO NOTHING;
    INSERT INTO legacy_search_configuration_publication_projections (
      legacy_revision_id, acquisition_policy_revision_id,
      qualification_definition_revision_id
    ) VALUES (
      legacy.revision_id, legacy.acquisition_policy_revision_id,
      legacy.qualification_definition_revision_id
    ) ON CONFLICT (legacy_revision_id) DO NOTHING;
    SELECT acquisition_policy_revision_id, qualification_definition_revision_id
    INTO stored_acquisition_id, stored_qualification_id
    FROM legacy_search_configuration_publication_projections
    WHERE legacy_revision_id = legacy.revision_id;
    IF stored_acquisition_id IS DISTINCT FROM legacy.acquisition_policy_revision_id
       OR stored_qualification_id IS DISTINCT FROM legacy.qualification_definition_revision_id
    THEN
      RAISE EXCEPTION 'publication projection differs for legacy revision %', legacy.revision_id;
    END IF;
  END LOOP;
END;
$$;

SELECT backfill_legacy_search_configuration_publications();

CREATE TABLE acquisition_policy_drafts (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  base_revision_id CHAR(64) NOT NULL REFERENCES acquisition_policy_publications(revision_id),
  version BIGINT NOT NULL CHECK (version >= 0),
  content JSONB NOT NULL CHECK (is_valid_acquisition_policy_content(content)),
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by TEXT NOT NULL CHECK (updated_by <> '')
);
CREATE TABLE qualification_definition_drafts (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  base_revision_id CHAR(64) NOT NULL
    REFERENCES qualification_definition_publications(revision_id),
  version BIGINT NOT NULL CHECK (version >= 0),
  content JSONB NOT NULL CHECK (is_valid_qualification_definition_content(content)),
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by TEXT NOT NULL CHECK (updated_by <> '')
);

CREATE FUNCTION enforce_split_policy_draft_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.singleton_id <> OLD.singleton_id OR NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION '% updates must increment version by one', TG_TABLE_NAME
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER acquisition_policy_draft_changes_are_versioned
BEFORE UPDATE OR DELETE ON acquisition_policy_drafts
FOR EACH ROW EXECUTE FUNCTION enforce_split_policy_draft_change();
CREATE TRIGGER qualification_definition_draft_changes_are_versioned
BEFORE UPDATE OR DELETE ON qualification_definition_drafts
FOR EACH ROW EXECUTE FUNCTION enforce_split_policy_draft_change();

INSERT INTO acquisition_policy_drafts (
  singleton_id, base_revision_id, version, content, updated_at, updated_by
)
SELECT draft.singleton_id, base.acquisition_policy_revision_id, 0,
       jsonb_build_object(
         'schema_version', 1,
         'search_keywords', draft.content -> 'search_keywords',
         'enabled_sources', draft.content -> 'enabled_sources'
       ), draft.updated_at, draft.updated_by
FROM search_configuration_drafts draft
JOIN legacy_search_configuration_publication_projections base
  ON base.legacy_revision_id = draft.base_revision_id;

INSERT INTO qualification_definition_drafts (
  singleton_id, base_revision_id, version, content, updated_at, updated_by
)
SELECT draft.singleton_id, base.qualification_definition_revision_id, 0,
       jsonb_build_object(
         'schema_version', 1,
         'personal_criteria', draft.content -> 'personal_criteria',
         'target_profiles', draft.content -> 'target_profiles'
       ), draft.updated_at, draft.updated_by
FROM search_configuration_drafts draft
JOIN legacy_search_configuration_publication_projections base
  ON base.legacy_revision_id = draft.base_revision_id;

CREATE TABLE active_acquisition_policy (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  revision_id CHAR(64) NOT NULL REFERENCES acquisition_policy_publications(revision_id),
  generation BIGINT NOT NULL CHECK (generation >= 0),
  activated_at TIMESTAMPTZ NOT NULL,
  activated_by TEXT NOT NULL CHECK (activated_by <> '')
);

INSERT INTO active_acquisition_policy (
  singleton_id, revision_id, generation, activated_at, activated_by
)
SELECT active.singleton_id, projection.acquisition_policy_revision_id, 0,
       active.activated_at, active.activated_by
FROM active_search_configuration active
JOIN legacy_search_configuration_publication_projections projection
  ON projection.legacy_revision_id = active.revision_id;

CREATE TABLE acquisition_policy_publication_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  outcome TEXT NOT NULL CHECK (outcome IN ('published', 'draft_changed')),
  expected_draft_version BIGINT NOT NULL CHECK (expected_draft_version >= 0),
  expected_revision_id CHAR(64) NOT NULL CHECK (expected_revision_id ~ '^[0-9a-f]{64}$'),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  observed_draft_version BIGINT CHECK (observed_draft_version >= 0),
  observed_revision_id CHAR(64) CHECK (observed_revision_id ~ '^[0-9a-f]{64}$'),
  publication_revision_id CHAR(64) REFERENCES acquisition_policy_publications(revision_id),
  rebased_draft_version BIGINT CHECK (rebased_draft_version >= 1),
  CONSTRAINT acquisition_policy_publication_receipt_shape CHECK (
    (outcome = 'published' AND publication_revision_id IS NOT NULL
      AND rebased_draft_version IS NOT NULL
      AND publication_revision_id = expected_revision_id
      AND rebased_draft_version = expected_draft_version + 1
      AND observed_draft_version IS NULL AND observed_revision_id IS NULL)
    OR (outcome = 'draft_changed' AND publication_revision_id IS NULL
      AND rebased_draft_version IS NULL AND observed_draft_version IS NOT NULL
      AND observed_revision_id IS NOT NULL
      AND (observed_draft_version <> expected_draft_version
        OR observed_revision_id <> expected_revision_id))
  )
);
CREATE TRIGGER acquisition_policy_publication_receipts_are_immutable
BEFORE UPDATE OR DELETE ON acquisition_policy_publication_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE qualification_definition_publication_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  outcome TEXT NOT NULL CHECK (outcome IN ('published', 'draft_changed')),
  expected_draft_version BIGINT NOT NULL CHECK (expected_draft_version >= 0),
  expected_revision_id CHAR(64) NOT NULL CHECK (expected_revision_id ~ '^[0-9a-f]{64}$'),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  observed_draft_version BIGINT CHECK (observed_draft_version >= 0),
  observed_revision_id CHAR(64) CHECK (observed_revision_id ~ '^[0-9a-f]{64}$'),
  publication_revision_id CHAR(64)
    REFERENCES qualification_definition_publications(revision_id),
  rebased_draft_version BIGINT CHECK (rebased_draft_version >= 1),
  CONSTRAINT qualification_definition_publication_receipt_shape CHECK (
    (outcome = 'published' AND publication_revision_id IS NOT NULL
      AND rebased_draft_version IS NOT NULL
      AND publication_revision_id = expected_revision_id
      AND rebased_draft_version = expected_draft_version + 1
      AND observed_draft_version IS NULL AND observed_revision_id IS NULL)
    OR (outcome = 'draft_changed' AND publication_revision_id IS NULL
      AND rebased_draft_version IS NULL AND observed_draft_version IS NOT NULL
      AND observed_revision_id IS NOT NULL
      AND (observed_draft_version <> expected_draft_version
        OR observed_revision_id <> expected_revision_id))
  )
);
CREATE TRIGGER qualification_definition_publication_receipts_are_immutable
BEFORE UPDATE OR DELETE ON qualification_definition_publication_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE acquisition_policy_activation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  outcome TEXT NOT NULL CHECK (outcome IN ('activated', 'active_changed')),
  candidate_revision_id CHAR(64) NOT NULL REFERENCES acquisition_policy_publications(revision_id),
  expected_revision_id CHAR(64) NOT NULL REFERENCES acquisition_policy_publications(revision_id),
  expected_generation BIGINT NOT NULL CHECK (expected_generation >= 0),
  observed_revision_id CHAR(64) NOT NULL REFERENCES acquisition_policy_publications(revision_id),
  observed_generation BIGINT NOT NULL CHECK (observed_generation >= 0),
  resulting_generation BIGINT NOT NULL CHECK (resulting_generation >= 0),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT acquisition_policy_activation_receipt_shape CHECK (
    (outcome = 'activated' AND observed_revision_id = expected_revision_id
      AND observed_generation = expected_generation
      AND resulting_generation = observed_generation + 1)
    OR (outcome = 'active_changed' AND resulting_generation = observed_generation
      AND (observed_revision_id <> expected_revision_id
        OR observed_generation <> expected_generation))
  )
);
CREATE TRIGGER acquisition_policy_activation_receipts_are_immutable
BEFORE UPDATE OR DELETE ON acquisition_policy_activation_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
CREATE UNIQUE INDEX acquisition_policy_activations_have_one_generation
ON acquisition_policy_activation_receipts (resulting_generation)
WHERE outcome = 'activated';

CREATE FUNCTION enforce_active_acquisition_policy_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'active_acquisition_policy rows cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.singleton_id <> OLD.singleton_id OR NEW.generation <> OLD.generation + 1 THEN
    RAISE EXCEPTION 'active acquisition updates must increment generation by one'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER active_acquisition_policy_changes_are_versioned
BEFORE UPDATE OR DELETE ON active_acquisition_policy
FOR EACH ROW EXECUTE FUNCTION enforce_active_acquisition_policy_change();

CREATE FUNCTION require_acquisition_policy_activation_receipt()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM acquisition_policy_activation_receipts receipt
    WHERE receipt.outcome = 'activated'
      AND receipt.expected_revision_id = OLD.revision_id
      AND receipt.candidate_revision_id = NEW.revision_id
      AND receipt.observed_revision_id = OLD.revision_id
      AND receipt.observed_generation = OLD.generation
      AND receipt.resulting_generation = NEW.generation
      AND receipt.actor = NEW.activated_by
      AND receipt.requested_at = NEW.activated_at
  ) THEN
    RAISE EXCEPTION 'active acquisition update requires an activation receipt'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER active_acquisition_policy_requires_receipt
AFTER UPDATE ON active_acquisition_policy
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_acquisition_policy_activation_receipt();
