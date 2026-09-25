CREATE FUNCTION is_valid_acquisition_policy_content(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
  IF jsonb_typeof(candidate) <> 'object'
    OR NOT candidate ?& ARRAY['schema_version', 'search_keywords', 'enabled_sources']
    OR candidate - ARRAY['schema_version', 'search_keywords', 'enabled_sources'] <> '{}'::JSONB
    OR jsonb_typeof(candidate -> 'schema_version') <> 'number'
    OR candidate ->> 'schema_version' <> '1'
    OR jsonb_typeof(candidate -> 'search_keywords') <> 'array'
    OR jsonb_array_length(candidate -> 'search_keywords') NOT BETWEEN 1 AND 100
    OR jsonb_typeof(candidate -> 'enabled_sources') <> 'array'
    OR jsonb_array_length(candidate -> 'enabled_sources') NOT BETWEEN 1 AND 4
  THEN
    RETURN FALSE;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(candidate -> 'search_keywords') AS keyword(value)
    WHERE jsonb_typeof(value) <> 'string'
      OR length(value #>> '{}') NOT BETWEEN 1 AND 200
      OR value #>> '{}' <> btrim(value #>> '{}', E' \t\n\r\f' || chr(11))
  ) OR (
    SELECT count(*) <> count(DISTINCT translate(
      value #>> '{}',
      'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
      'abcdefghijklmnopqrstuvwxyz'
    ))
    FROM jsonb_array_elements(candidate -> 'search_keywords') AS keyword(value)
  ) THEN
    RETURN FALSE;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(candidate -> 'enabled_sources') AS source(value)
    WHERE jsonb_typeof(value) <> 'string'
      OR value #>> '{}' NOT IN ('ashby', 'lever', 'greenhouse', 'workable')
  ) OR (
    SELECT count(*) <> count(DISTINCT value)
    FROM jsonb_array_elements(candidate -> 'enabled_sources') AS source(value)
  ) THEN
    RETURN FALSE;
  END IF;

  RETURN TRUE;
EXCEPTION WHEN OTHERS THEN
  RETURN FALSE;
END;
$$;

CREATE FUNCTION is_valid_qualification_definition_content(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  member JSONB;
BEGIN
  IF jsonb_typeof(candidate) <> 'object'
    OR NOT candidate ?& ARRAY['schema_version', 'personal_criteria', 'target_profiles']
    OR candidate - ARRAY['schema_version', 'personal_criteria', 'target_profiles'] <> '{}'::JSONB
    OR jsonb_typeof(candidate -> 'schema_version') <> 'number'
    OR candidate ->> 'schema_version' <> '1'
    OR jsonb_typeof(candidate -> 'personal_criteria') <> 'array'
    OR jsonb_array_length(candidate -> 'personal_criteria') NOT BETWEEN 1 AND 20
    OR jsonb_typeof(candidate -> 'target_profiles') <> 'array'
    OR jsonb_array_length(candidate -> 'target_profiles') NOT BETWEEN 1 AND 20
  THEN
    RETURN FALSE;
  END IF;

  FOR member IN SELECT value FROM jsonb_array_elements(candidate -> 'personal_criteria') LOOP
    IF NOT is_valid_search_configuration_member(member) THEN
      RETURN FALSE;
    END IF;
  END LOOP;
  FOR member IN SELECT value FROM jsonb_array_elements(candidate -> 'target_profiles') LOOP
    IF NOT is_valid_search_configuration_member(member) THEN
      RETURN FALSE;
    END IF;
  END LOOP;

  IF (
    SELECT count(*) <> count(DISTINCT value ->> 'key')
    FROM jsonb_array_elements(candidate -> 'personal_criteria') AS criterion(value)
  ) OR (
    SELECT count(*) <> count(DISTINCT value ->> 'key')
    FROM jsonb_array_elements(candidate -> 'target_profiles') AS profile(value)
  ) THEN
    RETURN FALSE;
  END IF;

  RETURN TRUE;
EXCEPTION WHEN OTHERS THEN
  RETURN FALSE;
END;
$$;

CREATE TABLE acquisition_policy_revisions (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  content JSONB NOT NULL CHECK (is_valid_acquisition_policy_content(content)),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT acquisition_policy_revision_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  )
);

CREATE TRIGGER acquisition_policy_revisions_are_immutable
BEFORE UPDATE OR DELETE ON acquisition_policy_revisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE qualification_definition_revisions (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  content JSONB NOT NULL CHECK (is_valid_qualification_definition_content(content)),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT qualification_definition_revision_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  )
);

CREATE TRIGGER qualification_definition_revisions_are_immutable
BEFORE UPDATE OR DELETE ON qualification_definition_revisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE legacy_search_configuration_policy_projections (
  legacy_revision_id CHAR(64) PRIMARY KEY REFERENCES search_configuration_revisions(id),
  acquisition_policy_revision_id CHAR(64) NOT NULL REFERENCES acquisition_policy_revisions(id),
  qualification_definition_revision_id CHAR(64) NOT NULL
    REFERENCES qualification_definition_revisions(id)
);

CREATE TRIGGER legacy_search_configuration_policy_projections_are_immutable
BEFORE UPDATE OR DELETE ON legacy_search_configuration_policy_projections
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION backfill_legacy_search_configuration_policies()
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  legacy RECORD;
  acquisition_content JSONB;
  qualification_content JSONB;
  acquisition_id CHAR(64);
  qualification_id CHAR(64);
  stored_content JSONB;
  stored_acquisition_id CHAR(64);
  stored_qualification_id CHAR(64);
BEGIN
  FOR legacy IN
    SELECT id, content, created_at, created_by
    FROM search_configuration_revisions
    ORDER BY id
  LOOP
    acquisition_content := jsonb_build_object(
      'schema_version', 1,
      'search_keywords', legacy.content -> 'search_keywords',
      'enabled_sources', legacy.content -> 'enabled_sources'
    );
    qualification_content := jsonb_build_object(
      'schema_version', 1,
      'personal_criteria', legacy.content -> 'personal_criteria',
      'target_profiles', legacy.content -> 'target_profiles'
    );
    acquisition_id := encode(
      sha256(convert_to(canonical_job_finder_json(acquisition_content), 'UTF8')),
      'hex'
    );
    qualification_id := encode(
      sha256(convert_to(canonical_job_finder_json(qualification_content), 'UTF8')),
      'hex'
    );

    INSERT INTO acquisition_policy_revisions (id, content, created_at, created_by)
    VALUES (acquisition_id, acquisition_content, legacy.created_at, legacy.created_by)
    ON CONFLICT (id) DO NOTHING;
    SELECT content INTO stored_content FROM acquisition_policy_revisions WHERE id = acquisition_id;
    IF stored_content IS DISTINCT FROM acquisition_content THEN
      RAISE EXCEPTION 'acquisition projection differs for legacy revision %', legacy.id;
    END IF;

    INSERT INTO qualification_definition_revisions (id, content, created_at, created_by)
    VALUES (qualification_id, qualification_content, legacy.created_at, legacy.created_by)
    ON CONFLICT (id) DO NOTHING;
    SELECT content INTO stored_content
    FROM qualification_definition_revisions WHERE id = qualification_id;
    IF stored_content IS DISTINCT FROM qualification_content THEN
      RAISE EXCEPTION 'qualification projection differs for legacy revision %', legacy.id;
    END IF;

    INSERT INTO legacy_search_configuration_policy_projections (
      legacy_revision_id, acquisition_policy_revision_id,
      qualification_definition_revision_id
    ) VALUES (legacy.id, acquisition_id, qualification_id)
    ON CONFLICT (legacy_revision_id) DO NOTHING;
    SELECT acquisition_policy_revision_id, qualification_definition_revision_id
    INTO stored_acquisition_id, stored_qualification_id
    FROM legacy_search_configuration_policy_projections
    WHERE legacy_revision_id = legacy.id;
    IF stored_acquisition_id IS DISTINCT FROM acquisition_id
       OR stored_qualification_id IS DISTINCT FROM qualification_id THEN
      RAISE EXCEPTION 'policy projection mapping differs for legacy revision %', legacy.id;
    END IF;
  END LOOP;
END;
$$;

SELECT backfill_legacy_search_configuration_policies();
