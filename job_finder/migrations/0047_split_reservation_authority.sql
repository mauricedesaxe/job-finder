ALTER TABLE execution_budget_reservations
DROP CONSTRAINT execution_budget_reservations_authority_kind_check,
DROP CONSTRAINT execution_budget_reservation_authority_is_complete,
ADD COLUMN acquisition_policy_revision_id CHAR(64)
  REFERENCES acquisition_policy_revisions(id),
ADD COLUMN qualification_target_id CHAR(64)
  REFERENCES qualification_targets(id),
ADD COLUMN acquisition_generation BIGINT CHECK (acquisition_generation >= 0),
ADD COLUMN qualification_generation BIGINT CHECK (qualification_generation >= 0),
ADD CONSTRAINT execution_budget_reservations_authority_kind_check CHECK (
  authority_kind IN ('legacy', 'adopted', 'pinned', 'split')
),
ADD CONSTRAINT execution_budget_reservation_authority_is_complete CHECK (
  (
    authority_kind = 'legacy'
    AND configuration_revision_id IS NULL
    AND prompt_release_id IS NULL
    AND relevance_release_id IS NULL
    AND release_generation IS NULL
    AND acquisition_policy_revision_id IS NULL
    AND qualification_target_id IS NULL
    AND acquisition_generation IS NULL
    AND qualification_generation IS NULL
    AND search_queries IS NULL
    AND logical_model_calls_per_job IS NULL
    AND maximum_provider_attempts IS NULL
  ) OR (
    authority_kind IN ('adopted', 'pinned')
    AND configuration_revision_id IS NOT NULL
    AND prompt_release_id IS NOT NULL
    AND relevance_release_id IS NOT NULL
    AND (authority_kind = 'adopted' OR release_generation IS NOT NULL)
    AND (authority_kind = 'pinned' OR release_generation IS NULL)
    AND acquisition_policy_revision_id IS NULL
    AND qualification_target_id IS NULL
    AND acquisition_generation IS NULL
    AND qualification_generation IS NULL
    AND search_queries IS NOT NULL
    AND logical_model_calls_per_job IS NOT NULL
    AND maximum_provider_attempts IS NOT NULL
  ) OR (
    authority_kind = 'split'
    AND configuration_revision_id IS NULL
    AND prompt_release_id IS NULL
    AND relevance_release_id IS NULL
    AND release_generation IS NULL
    AND acquisition_policy_revision_id IS NOT NULL
    AND qualification_target_id IS NOT NULL
    AND acquisition_generation IS NOT NULL
    AND qualification_generation IS NOT NULL
    AND search_queries IS NOT NULL
    AND logical_model_calls_per_job IS NOT NULL
    AND maximum_provider_attempts IS NOT NULL
  )
),
ADD CONSTRAINT execution_budget_reservation_matches_split_run
FOREIGN KEY (pipeline_run_id, acquisition_policy_revision_id, qualification_target_id)
REFERENCES pipeline_runs (id, acquisition_policy_revision_id, qualification_target_id);

CREATE OR REPLACE FUNCTION protect_execution_budget_reservation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'execution budget reservation cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status <> 'reserved' OR NEW.status NOT IN ('reserved', 'settled') THEN
    RAISE EXCEPTION 'invalid execution budget reservation transition'
      USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.authority_kind <> OLD.authority_kind
      OR NEW.configuration_revision_id IS DISTINCT FROM OLD.configuration_revision_id
      OR NEW.prompt_release_id IS DISTINCT FROM OLD.prompt_release_id
      OR NEW.relevance_release_id IS DISTINCT FROM OLD.relevance_release_id
      OR NEW.release_generation IS DISTINCT FROM OLD.release_generation
      OR NEW.acquisition_policy_revision_id IS DISTINCT FROM OLD.acquisition_policy_revision_id
      OR NEW.qualification_target_id IS DISTINCT FROM OLD.qualification_target_id
      OR NEW.acquisition_generation IS DISTINCT FROM OLD.acquisition_generation
      OR NEW.qualification_generation IS DISTINCT FROM OLD.qualification_generation
      OR NEW.search_queries IS DISTINCT FROM OLD.search_queries
      OR NEW.logical_model_calls_per_job IS DISTINCT FROM OLD.logical_model_calls_per_job
      OR NEW.maximum_provider_attempts IS DISTINCT FROM OLD.maximum_provider_attempts)
     AND NOT (
       OLD.authority_kind = 'legacy'
       AND NEW.authority_kind IN ('adopted', 'pinned')
     ) THEN
    RAISE EXCEPTION 'execution budget reservation authority is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.idempotency_key <> OLD.idempotency_key
     OR NEW.policy_version <> OLD.policy_version
     OR NEW.period_start <> OLD.period_start
     OR NEW.reserved_usd <> OLD.reserved_usd
     OR NEW.max_jobs <> OLD.max_jobs
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'execution budget reservation authority is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.jobs_reserved < OLD.jobs_reserved
     OR (OLD.discovery_reserved AND NOT NEW.discovery_reserved) THEN
    RAISE EXCEPTION 'execution budget capacity cannot be restored'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION require_run_budget_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  reservation_authority TEXT;
  reservation_configuration CHAR(64);
  reservation_prompt CHAR(64);
  reservation_relevance CHAR(64);
  reservation_acquisition CHAR(64);
  reservation_qualification CHAR(64);
BEGIN
  IF NEW.kind <> 'orchestration' THEN
    RETURN NEW;
  END IF;
  SELECT authority_kind, configuration_revision_id, prompt_release_id,
         relevance_release_id, acquisition_policy_revision_id, qualification_target_id
  INTO reservation_authority, reservation_configuration, reservation_prompt,
       reservation_relevance, reservation_acquisition, reservation_qualification
  FROM execution_budget_reservations
  WHERE idempotency_key = NEW.idempotency_key
  FOR UPDATE;
  IF NEW.execution_authority_kind = 'split' THEN
    IF reservation_authority IS DISTINCT FROM 'split'
       OR (NEW.acquisition_policy_revision_id, NEW.qualification_target_id)
          IS DISTINCT FROM (reservation_acquisition, reservation_qualification) THEN
      RAISE EXCEPTION 'pipeline run differs from reserved split execution authority'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF reservation_authority IN ('adopted', 'pinned')
     AND (NEW.configuration_revision_id, NEW.prompt_release_id,
          NEW.relevance_release_id) IS DISTINCT FROM
         (reservation_configuration, reservation_prompt, reservation_relevance) THEN
    RAISE EXCEPTION 'pipeline run differs from reserved execution authority'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
