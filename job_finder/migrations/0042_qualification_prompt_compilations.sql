CREATE TABLE qualification_prompt_compilations (
  target_id CHAR(64) PRIMARY KEY REFERENCES qualification_targets(id),
  artifact_id CHAR(64) NOT NULL REFERENCES implementation_artifacts(id),
  qualification_definition_revision_id CHAR(64) NOT NULL
    REFERENCES qualification_definition_publications(revision_id),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> '')
);
CREATE TRIGGER qualification_prompt_compilations_are_immutable
BEFORE UPDATE OR DELETE ON qualification_prompt_compilations
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_qualification_prompt_compilation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  target RECORD;
  expected_versions JSONB;
  actual_versions JSONB;
BEGIN
  SELECT t.artifact_id, t.qualification_definition_revision_id,
         relevance.content -> 'prompt_version_ids' AS relevance_versions,
         enrichment.content ->> 'prompt_version_id' AS enrichment_version,
         deduplication.content ->> 'prompt_version_id' AS deduplication_version
    INTO target
  FROM qualification_targets t
  JOIN qualification_component_releases relevance ON relevance.id = t.relevance_release_id
  JOIN qualification_component_releases enrichment ON enrichment.id = t.enrichment_release_id
  JOIN qualification_component_releases deduplication ON deduplication.id = t.deduplication_release_id
  WHERE t.id = NEW.target_id;
  IF target.artifact_id IS DISTINCT FROM NEW.artifact_id
    OR target.qualification_definition_revision_id
      IS DISTINCT FROM NEW.qualification_definition_revision_id
  THEN
    RAISE EXCEPTION 'prompt compilation must match its target definition and artifact'
      USING ERRCODE = 'check_violation';
  END IF;
  expected_versions := target.relevance_versions || jsonb_build_array(
    target.enrichment_version, target.deduplication_version
  );
  SELECT jsonb_agg(member.prompt_version_id ORDER BY member.position)
    INTO actual_versions
  FROM prompt_release_members member
  WHERE member.release_id = NEW.prompt_release_id;
  IF actual_versions IS DISTINCT FROM expected_versions THEN
    RAISE EXCEPTION 'prompt compilation must contain the exact ordered target prompts'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER qualification_prompt_compilations_match_target
BEFORE INSERT ON qualification_prompt_compilations
FOR EACH ROW EXECUTE FUNCTION require_qualification_prompt_compilation();
