CREATE TABLE qualification_provider_attempts (
  id UUID PRIMARY KEY,
  evidence_id CHAR(64) NOT NULL REFERENCES qualification_phase_evidence(id),
  request_id CHAR(64) NOT NULL CHECK (request_id ~ '^[0-9a-f]{64}$'),
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  prompt_name TEXT NOT NULL,
  prompt_version_id CHAR(64) NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN ('openrouter', 'typesafe')),
  content JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  UNIQUE (evidence_id, request_id, attempt_number),
  FOREIGN KEY (prompt_release_id, prompt_name, prompt_version_id)
    REFERENCES prompt_release_members(release_id, prompt_name, prompt_version_id),
  CHECK (
    content ->> 'id' = id::TEXT
    AND content ->> 'request_id' = request_id
    AND (content ->> 'attempt_number')::INTEGER = attempt_number
    AND content ->> 'prompt_name' = prompt_name
    AND content ->> 'prompt_version_id' = prompt_version_id
    AND content #>> '{context,prompt_release_id}' = prompt_release_id
  )
);
CREATE TRIGGER qualification_provider_attempts_are_immutable
BEFORE UPDATE OR DELETE ON qualification_provider_attempts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_qualification_provider_attempt_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  evidence RECORD;
BEGIN
  SELECT e.phase, e.origin, compilation.prompt_release_id
    INTO evidence
  FROM qualification_phase_evidence e
  JOIN qualification_prompt_compilations compilation
    ON compilation.target_id = e.target_id
  WHERE e.id = NEW.evidence_id;
  IF evidence.phase NOT IN ('relevance', 'enrichment', 'deduplication', 'composition')
    OR evidence.origin NOT IN ('canonical', 'synthetic')
    OR evidence.prompt_release_id IS DISTINCT FROM NEW.prompt_release_id
  THEN
    RAISE EXCEPTION 'provider attempt requires executed evidence for its compiled target'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER qualification_provider_attempts_match_target
BEFORE INSERT ON qualification_provider_attempts
FOR EACH ROW EXECUTE FUNCTION require_qualification_provider_attempt_target();
