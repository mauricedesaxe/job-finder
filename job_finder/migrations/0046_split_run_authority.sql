ALTER TABLE pipeline_runs
ADD COLUMN execution_authority_kind TEXT NOT NULL DEFAULT 'legacy'
  CHECK (execution_authority_kind IN ('legacy', 'split')),
ADD COLUMN acquisition_policy_revision_id CHAR(64)
  REFERENCES acquisition_policy_revisions(id),
ADD COLUMN qualification_target_id CHAR(64)
  REFERENCES qualification_targets(id),
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
    AND prompt_release_id IS NULL
    AND relevance_release_id IS NULL
  )
),
ADD CONSTRAINT pipeline_run_split_authority_key UNIQUE (
  id, acquisition_policy_revision_id, qualification_target_id
);

CREATE OR REPLACE FUNCTION default_legacy_orchestration_run_configuration()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.kind = 'orchestration'
     AND NEW.execution_authority_kind = 'legacy'
     AND NEW.configuration_revision_id IS NULL THEN
    SELECT min(revision_id::TEXT)::CHAR(64) INTO NEW.configuration_revision_id
    FROM search_configuration_publications
    WHERE prompt_release_id = NEW.prompt_release_id
    HAVING count(*) = 1;

    IF NEW.configuration_revision_id IS NULL THEN
      RAISE EXCEPTION 'cannot infer configuration revision for prompt release %',
        NEW.prompt_release_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

ALTER TABLE pipeline_runs
DROP CONSTRAINT orchestration_runs_own_complete_release_target,
ADD CONSTRAINT orchestration_runs_own_complete_release_target CHECK (
  kind NOT IN ('orchestration', 'reevaluation', 'onboarding')
  OR execution_authority_kind = 'split'
  OR (prompt_release_id IS NOT NULL AND relevance_release_id IS NOT NULL)
) NOT VALID;

CREATE FUNCTION protect_pipeline_run_execution_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.execution_authority_kind IS DISTINCT FROM OLD.execution_authority_kind
     OR NEW.acquisition_policy_revision_id IS DISTINCT FROM OLD.acquisition_policy_revision_id
     OR NEW.qualification_target_id IS DISTINCT FROM OLD.qualification_target_id THEN
    RAISE EXCEPTION 'pipeline run execution authority is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER pipeline_run_execution_authority_is_immutable
BEFORE UPDATE ON pipeline_runs
FOR EACH ROW EXECUTE FUNCTION protect_pipeline_run_execution_authority();

CREATE OR REPLACE FUNCTION require_complete_orchestration_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  checked_run_id UUID;
  run_kind TEXT;
  authority_kind TEXT;
  prompt_id CHAR(64);
  relevance_id CHAR(64);
  acquisition_id CHAR(64);
  qualification_id CHAR(64);
  snapshot_count INTEGER;
BEGIN
  IF TG_TABLE_NAME = 'pipeline_runs' THEN
    checked_run_id := NEW.id;
  ELSE
    checked_run_id := COALESCE(NEW.pipeline_run_id, OLD.pipeline_run_id);
  END IF;
  SELECT kind, execution_authority_kind, prompt_release_id, relevance_release_id,
         acquisition_policy_revision_id, qualification_target_id
  INTO run_kind, authority_kind, prompt_id, relevance_id,
       acquisition_id, qualification_id
  FROM pipeline_runs WHERE id = checked_run_id;
  IF run_kind IS NULL OR run_kind NOT IN ('orchestration', 'reevaluation', 'onboarding') THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO snapshot_count
  FROM run_exchange_rate_snapshots WHERE pipeline_run_id = checked_run_id;
  IF snapshot_count <> 1
     OR (authority_kind = 'legacy' AND (prompt_id IS NULL OR relevance_id IS NULL))
     OR (authority_kind = 'split' AND (acquisition_id IS NULL OR qualification_id IS NULL)) THEN
    RAISE EXCEPTION '% run % requires complete authority and one rate snapshot',
      run_kind, checked_run_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;
