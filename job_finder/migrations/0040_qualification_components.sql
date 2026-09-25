CREATE TABLE implementation_artifacts (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  manifest JSONB NOT NULL CHECK (
    COALESCE(jsonb_typeof(manifest) = 'object'
    AND manifest ?& ARRAY[
      'schema_version', 'runtime', 'dependency_lock_sha256',
      'entrypoints', 'source_files'
    ]
    AND manifest - ARRAY[
      'schema_version', 'runtime', 'dependency_lock_sha256',
      'entrypoints', 'source_files'
    ] = '{}'::JSONB
    AND jsonb_typeof(manifest -> 'schema_version') = 'number'
    AND manifest ->> 'schema_version' = '1'
    AND jsonb_typeof(manifest -> 'runtime') = 'string'
    AND jsonb_typeof(manifest -> 'entrypoints') = 'array'
    AND jsonb_typeof(manifest -> 'source_files') = 'array'
    AND jsonb_array_length(manifest -> 'source_files') > 0
    AND manifest ->> 'dependency_lock_sha256' ~ '^[0-9a-f]{64}$', FALSE)
  ),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT implementation_artifact_matches_manifest CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(manifest), 'UTF8')), 'hex')
  )
);
CREATE TRIGGER implementation_artifacts_are_immutable
BEFORE UPDATE OR DELETE ON implementation_artifacts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION is_valid_qualification_component(kind TEXT, content JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
  IF jsonb_typeof(content) <> 'object'
    OR content ->> 'kind' IS DISTINCT FROM kind
    OR jsonb_typeof(content -> 'schema_version') <> 'number'
    OR content ->> 'schema_version' <> '1'
    OR content ->> 'artifact_id' !~ '^[0-9a-f]{64}$'
  THEN
    RETURN FALSE;
  END IF;
  CASE kind
    WHEN 'input_preparation' THEN
      RETURN content ?& ARRAY[
        'kind', 'schema_version', 'artifact_id', 'ats_sources', 'contract'
      ] AND content - ARRAY[
        'kind', 'schema_version', 'artifact_id', 'ats_sources', 'contract'
      ] = '{}'::JSONB
        AND jsonb_typeof(content -> 'ats_sources') = 'array'
        AND jsonb_array_length(content -> 'ats_sources') > 0
        AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements_text(content -> 'ats_sources') AS source(value)
          WHERE source.value NOT IN ('ashby', 'lever', 'greenhouse', 'workable')
        )
        AND (
          SELECT count(*) = count(DISTINCT source.value)
          FROM jsonb_array_elements_text(content -> 'ats_sources') AS source(value)
        )
        AND content ->> 'contract' = 'ats-adapter-parser-structural-v1';
    WHEN 'relevance' THEN
      RETURN content ?& ARRAY[
        'kind', 'schema_version', 'artifact_id',
        'qualification_definition_revision_id', 'relevance_release_id',
        'prompt_version_ids', 'contract'
      ] AND content - ARRAY[
        'kind', 'schema_version', 'artifact_id',
        'qualification_definition_revision_id', 'relevance_release_id',
        'prompt_version_ids', 'contract'
      ] = '{}'::JSONB
        AND content ->> 'qualification_definition_revision_id' ~ '^[0-9a-f]{64}$'
        AND content ->> 'relevance_release_id' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(content -> 'prompt_version_ids') = 'array'
        AND jsonb_array_length(content -> 'prompt_version_ids') > 0
        AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements_text(content -> 'prompt_version_ids') AS version(id)
          WHERE version.id !~ '^[0-9a-f]{64}$'
        )
        AND (
          SELECT count(*) = count(DISTINCT version.id)
          FROM jsonb_array_elements_text(content -> 'prompt_version_ids') AS version(id)
        )
        AND content ->> 'contract' = 'filter-profile-composition-v1';
    WHEN 'enrichment' THEN
      RETURN content ?& ARRAY[
        'kind', 'schema_version', 'artifact_id', 'prompt_version_id',
        'output_schema_digest', 'contract'
      ] AND content - ARRAY[
        'kind', 'schema_version', 'artifact_id', 'prompt_version_id',
        'output_schema_digest', 'contract'
      ] = '{}'::JSONB
        AND content ->> 'prompt_version_id' ~ '^[0-9a-f]{64}$'
        AND content ->> 'output_schema_digest' ~ '^[0-9a-f]{64}$'
        AND content ->> 'contract' = 'canonical-job-fields-v1';
    WHEN 'deduplication' THEN
      RETURN content ?& ARRAY[
        'kind', 'schema_version', 'artifact_id', 'prompt_version_id', 'contract'
      ] AND content - ARRAY[
        'kind', 'schema_version', 'artifact_id', 'prompt_version_id', 'contract'
      ] = '{}'::JSONB
        AND content ->> 'prompt_version_id' ~ '^[0-9a-f]{64}$'
        AND content ->> 'contract' = 'company-title-ledger-fallback-v1';
    ELSE
      RETURN FALSE;
  END CASE;
EXCEPTION WHEN OTHERS THEN
  RETURN FALSE;
END;
$$;

CREATE TABLE qualification_component_releases (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  kind TEXT NOT NULL CHECK (
    kind IN ('input_preparation', 'relevance', 'enrichment', 'deduplication')
  ),
  artifact_id CHAR(64) NOT NULL REFERENCES implementation_artifacts(id),
  content JSONB NOT NULL CHECK (is_valid_qualification_component(kind, content) IS TRUE),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT qualification_component_identity UNIQUE (id, kind),
  CONSTRAINT qualification_component_artifact_matches_content CHECK (
    content ->> 'artifact_id' = artifact_id
  ),
  CONSTRAINT qualification_component_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  )
);
CREATE TRIGGER qualification_component_releases_are_immutable
BEFORE UPDATE OR DELETE ON qualification_component_releases
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_qualification_component_references()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_versions INTEGER;
  present_versions INTEGER;
BEGIN
  CASE NEW.kind
    WHEN 'relevance' THEN
      IF NOT EXISTS (
        SELECT 1 FROM qualification_definition_publications
        WHERE revision_id = NEW.content ->> 'qualification_definition_revision_id'
      ) OR NOT EXISTS (
        SELECT 1 FROM relevance_releases
        WHERE id = NEW.content ->> 'relevance_release_id'
      ) THEN
        RAISE EXCEPTION 'relevance component requires published definition and release'
          USING ERRCODE = 'check_violation';
      END IF;
      expected_versions := jsonb_array_length(NEW.content -> 'prompt_version_ids');
      SELECT count(*) INTO present_versions
      FROM prompt_versions version
      JOIN jsonb_array_elements_text(NEW.content -> 'prompt_version_ids') selected(id)
        ON version.id = selected.id
      WHERE version.phase IN ('filter', 'profile');
      IF present_versions <> expected_versions THEN
        RAISE EXCEPTION 'relevance component requires stored filter and profile prompts'
          USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'enrichment' THEN
      IF NOT EXISTS (
        SELECT 1 FROM prompt_versions
        WHERE id = NEW.content ->> 'prompt_version_id'
          AND phase = 'enrichment'
          AND encode(
            sha256(convert_to(canonical_job_finder_json(output_schema), 'UTF8')),
            'hex'
          ) = NEW.content ->> 'output_schema_digest'
      ) THEN
        RAISE EXCEPTION 'enrichment component requires its stored prompt and output schema'
          USING ERRCODE = 'check_violation';
      END IF;
    WHEN 'deduplication' THEN
      IF NOT EXISTS (
        SELECT 1 FROM prompt_versions
        WHERE id = NEW.content ->> 'prompt_version_id' AND phase = 'deduplication'
      ) THEN
        RAISE EXCEPTION 'deduplication component requires its stored prompt'
          USING ERRCODE = 'check_violation';
      END IF;
    ELSE
      NULL;
  END CASE;
  RETURN NEW;
END;
$$;
CREATE TRIGGER qualification_component_references_exist
BEFORE INSERT ON qualification_component_releases
FOR EACH ROW EXECUTE FUNCTION require_qualification_component_references();

CREATE FUNCTION is_valid_qualification_target_content(content JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
  RETURN jsonb_typeof(content) = 'object'
    AND content ?& ARRAY[
      'schema_version', 'artifact_id', 'qualification_definition_revision_id',
      'input_preparation_release_id', 'relevance_release_id',
      'enrichment_release_id', 'deduplication_release_id', 'composition_contract'
    ]
    AND content - ARRAY[
      'schema_version', 'artifact_id', 'qualification_definition_revision_id',
      'input_preparation_release_id', 'relevance_release_id',
      'enrichment_release_id', 'deduplication_release_id', 'composition_contract'
    ] = '{}'::JSONB
    AND jsonb_typeof(content -> 'schema_version') = 'number'
    AND content ->> 'schema_version' = '1'
    AND content ->> 'artifact_id' ~ '^[0-9a-f]{64}$'
    AND content ->> 'qualification_definition_revision_id' ~ '^[0-9a-f]{64}$'
    AND content ->> 'input_preparation_release_id' ~ '^[0-9a-f]{64}$'
    AND content ->> 'relevance_release_id' ~ '^[0-9a-f]{64}$'
    AND content ->> 'enrichment_release_id' ~ '^[0-9a-f]{64}$'
    AND content ->> 'deduplication_release_id' ~ '^[0-9a-f]{64}$'
    AND content ->> 'composition_contract' =
      'prepared-relevant-enriched-deduplicated-v1';
EXCEPTION WHEN OTHERS THEN
  RETURN FALSE;
END;
$$;

CREATE TABLE qualification_targets (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  artifact_id CHAR(64) NOT NULL REFERENCES implementation_artifacts(id),
  qualification_definition_revision_id CHAR(64) NOT NULL
    REFERENCES qualification_definition_publications(revision_id),
  input_preparation_release_id CHAR(64) NOT NULL
    REFERENCES qualification_component_releases(id),
  relevance_release_id CHAR(64) NOT NULL REFERENCES qualification_component_releases(id),
  enrichment_release_id CHAR(64) NOT NULL REFERENCES qualification_component_releases(id),
  deduplication_release_id CHAR(64) NOT NULL
    REFERENCES qualification_component_releases(id),
  content JSONB NOT NULL CHECK (is_valid_qualification_target_content(content) IS TRUE),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT qualification_target_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  ),
  CONSTRAINT qualification_target_columns_match_content CHECK (
    artifact_id = content ->> 'artifact_id'
    AND qualification_definition_revision_id =
      content ->> 'qualification_definition_revision_id'
    AND input_preparation_release_id = content ->> 'input_preparation_release_id'
    AND relevance_release_id = content ->> 'relevance_release_id'
    AND enrichment_release_id = content ->> 'enrichment_release_id'
    AND deduplication_release_id = content ->> 'deduplication_release_id'
  )
);
CREATE TRIGGER qualification_targets_are_immutable
BEFORE UPDATE OR DELETE ON qualification_targets
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_complete_qualification_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  input_component RECORD;
  relevance_component RECORD;
  enrichment_component RECORD;
  deduplication_component RECORD;
BEGIN
  SELECT kind, artifact_id, content INTO input_component
  FROM qualification_component_releases WHERE id = NEW.input_preparation_release_id;
  SELECT kind, artifact_id, content INTO relevance_component
  FROM qualification_component_releases WHERE id = NEW.relevance_release_id;
  SELECT kind, artifact_id, content INTO enrichment_component
  FROM qualification_component_releases WHERE id = NEW.enrichment_release_id;
  SELECT kind, artifact_id, content INTO deduplication_component
  FROM qualification_component_releases WHERE id = NEW.deduplication_release_id;
  IF input_component.kind IS DISTINCT FROM 'input_preparation'
    OR relevance_component.kind IS DISTINCT FROM 'relevance'
    OR enrichment_component.kind IS DISTINCT FROM 'enrichment'
    OR deduplication_component.kind IS DISTINCT FROM 'deduplication'
    OR input_component.artifact_id IS DISTINCT FROM NEW.artifact_id
    OR relevance_component.artifact_id IS DISTINCT FROM NEW.artifact_id
    OR enrichment_component.artifact_id IS DISTINCT FROM NEW.artifact_id
    OR deduplication_component.artifact_id IS DISTINCT FROM NEW.artifact_id
    OR relevance_component.content ->> 'qualification_definition_revision_id'
      IS DISTINCT FROM NEW.qualification_definition_revision_id
  THEN
    RAISE EXCEPTION 'qualification target requires four matching component releases'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER qualification_targets_require_complete_components
BEFORE INSERT ON qualification_targets
FOR EACH ROW EXECUTE FUNCTION require_complete_qualification_target();
