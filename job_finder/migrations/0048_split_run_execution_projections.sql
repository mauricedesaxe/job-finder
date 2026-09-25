ALTER TABLE qualification_prompt_compilations
ADD CONSTRAINT qualification_prompt_compilation_target_release_key
UNIQUE (target_id, prompt_release_id);

ALTER TABLE pipeline_runs
DROP CONSTRAINT pipeline_run_authority_is_complete,
ADD CONSTRAINT pipeline_run_authority_is_complete CHECK (
  (
    execution_authority_kind = 'legacy'
    AND acquisition_policy_revision_id IS NULL
    AND qualification_target_id IS NULL
  ) OR (
    execution_authority_kind = 'split'
    AND kind IN ('orchestration', 'reevaluation', 'onboarding')
    AND acquisition_policy_revision_id IS NOT NULL
    AND qualification_target_id IS NOT NULL
    AND configuration_revision_id IS NULL
    AND prompt_release_id IS NOT NULL
    AND relevance_release_id IS NOT NULL
  )
),
ADD CONSTRAINT split_run_prompt_release_matches_target
FOREIGN KEY (qualification_target_id, prompt_release_id)
REFERENCES qualification_prompt_compilations (target_id, prompt_release_id);

CREATE FUNCTION require_split_run_relevance_projection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_relevance_release_id CHAR(64);
BEGIN
  IF NEW.execution_authority_kind <> 'split' THEN
    RETURN NEW;
  END IF;
  SELECT relevance.content ->> 'relevance_release_id'
  INTO expected_relevance_release_id
  FROM qualification_targets target
  JOIN qualification_component_releases relevance
    ON relevance.id = target.relevance_release_id
  WHERE target.id = NEW.qualification_target_id;
  IF NEW.relevance_release_id IS DISTINCT FROM expected_relevance_release_id THEN
    RAISE EXCEPTION 'split run relevance release differs from qualification target'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER split_run_relevance_projection_matches_target
BEFORE INSERT OR UPDATE ON pipeline_runs
FOR EACH ROW EXECUTE FUNCTION require_split_run_relevance_projection();
