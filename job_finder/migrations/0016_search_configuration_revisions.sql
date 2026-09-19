CREATE FUNCTION canonical_job_finder_json(candidate JSONB)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
  result TEXT;
BEGIN
  CASE jsonb_typeof(candidate)
    WHEN 'object' THEN
      SELECT '{' || COALESCE(
        string_agg(
          to_jsonb(entry_key)::TEXT || ':' || canonical_job_finder_json(entry_value),
          ',' ORDER BY entry_key COLLATE "C"
        ),
        ''
      ) || '}'
      INTO result
      FROM jsonb_each(candidate) AS entry(entry_key, entry_value);
    WHEN 'array' THEN
      SELECT '[' || COALESCE(
        string_agg(canonical_job_finder_json(entry_value), ',' ORDER BY ordinal),
        ''
      ) || ']'
      INTO result
      FROM jsonb_array_elements(candidate) WITH ORDINALITY AS entry(entry_value, ordinal);
    ELSE
      result := candidate::TEXT;
  END CASE;
  RETURN result;
END;
$$;

CREATE FUNCTION is_valid_search_configuration_member(member JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT jsonb_typeof(member) = 'object'
    AND member ?& ARRAY['key', 'name', 'instructions']
    AND member - ARRAY['key', 'name', 'instructions'] = '{}'::JSONB
    AND jsonb_typeof(member -> 'key') = 'string'
    AND member ->> 'key' ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
    AND length(member ->> 'key') BETWEEN 1 AND 100
    AND jsonb_typeof(member -> 'name') = 'string'
    AND length(member ->> 'name') BETWEEN 1 AND 100
    AND member ->> 'name' = btrim(member ->> 'name', E' \t\n\r\f' || chr(11))
    AND jsonb_typeof(member -> 'instructions') = 'string'
    AND length(member ->> 'instructions') BETWEEN 1 AND 20000
    AND member ->> 'instructions' = btrim(
      member ->> 'instructions',
      E' \t\n\r\f' || chr(11)
    )
$$;

CREATE FUNCTION is_valid_search_configuration_content(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  member JSONB;
BEGIN
  IF jsonb_typeof(candidate) <> 'object'
    OR NOT candidate ?& ARRAY[
      'schema_version',
      'search_keywords',
      'enabled_sources',
      'personal_criteria',
      'target_profiles'
    ]
    OR candidate - ARRAY[
      'schema_version',
      'search_keywords',
      'enabled_sources',
      'personal_criteria',
      'target_profiles'
    ] <> '{}'::JSONB
    OR jsonb_typeof(candidate -> 'schema_version') <> 'number'
    OR candidate ->> 'schema_version' <> '1'
    OR jsonb_typeof(candidate -> 'search_keywords') <> 'array'
    OR jsonb_array_length(candidate -> 'search_keywords') NOT BETWEEN 1 AND 100
    OR jsonb_typeof(candidate -> 'enabled_sources') <> 'array'
    OR jsonb_array_length(candidate -> 'enabled_sources') NOT BETWEEN 1 AND 4
    OR jsonb_typeof(candidate -> 'personal_criteria') <> 'array'
    OR jsonb_array_length(candidate -> 'personal_criteria') NOT BETWEEN 1 AND 20
    OR jsonb_typeof(candidate -> 'target_profiles') <> 'array'
    OR jsonb_array_length(candidate -> 'target_profiles') NOT BETWEEN 1 AND 20
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

CREATE TABLE search_configuration_revisions (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  content JSONB NOT NULL CHECK (is_valid_search_configuration_content(content)),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT search_configuration_revision_matches_content CHECK (
    id = encode(
      sha256(convert_to(canonical_job_finder_json(content), 'UTF8')),
      'hex'
    )
  )
);

CREATE TRIGGER search_configuration_revisions_are_immutable
BEFORE UPDATE OR DELETE ON search_configuration_revisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE search_configuration_drafts (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  base_revision_id CHAR(64) NOT NULL REFERENCES search_configuration_revisions(id),
  version BIGINT NOT NULL CHECK (version >= 0),
  content JSONB NOT NULL CHECK (is_valid_search_configuration_content(content)),
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by TEXT NOT NULL CHECK (updated_by <> '')
);

CREATE FUNCTION enforce_search_configuration_draft_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'search_configuration_drafts rows cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.singleton_id <> OLD.singleton_id OR NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION 'search configuration draft updates must increment version by one'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER search_configuration_draft_changes_are_versioned
BEFORE UPDATE OR DELETE ON search_configuration_drafts
FOR EACH ROW EXECUTE FUNCTION enforce_search_configuration_draft_change();

CREATE TABLE active_search_configuration (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  revision_id CHAR(64) NOT NULL REFERENCES search_configuration_revisions(id),
  generation BIGINT NOT NULL CHECK (generation >= 0),
  activated_at TIMESTAMPTZ NOT NULL,
  activated_by TEXT NOT NULL CHECK (activated_by <> '')
);

CREATE FUNCTION enforce_active_search_configuration_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'active_search_configuration rows cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.singleton_id <> OLD.singleton_id OR NEW.generation <> OLD.generation + 1 THEN
    RAISE EXCEPTION 'active search configuration updates must increment generation by one'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER active_search_configuration_changes_are_versioned
BEFORE UPDATE OR DELETE ON active_search_configuration
FOR EACH ROW EXECUTE FUNCTION enforce_active_search_configuration_change();
