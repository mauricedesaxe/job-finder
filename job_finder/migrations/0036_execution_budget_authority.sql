ALTER TABLE execution_budget_reservations
ADD COLUMN authority_kind TEXT NOT NULL DEFAULT 'legacy'
  CHECK (authority_kind IN ('legacy', 'adopted', 'pinned')),
ADD COLUMN configuration_revision_id CHAR(64)
  REFERENCES search_configuration_revisions(id),
ADD COLUMN prompt_release_id CHAR(64) REFERENCES prompt_releases(id),
ADD COLUMN relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
ADD COLUMN release_generation BIGINT CHECK (release_generation >= 0),
ADD COLUMN search_queries INTEGER CHECK (search_queries >= 1),
ADD COLUMN logical_model_calls_per_job INTEGER
  CHECK (logical_model_calls_per_job >= 1),
ADD COLUMN maximum_provider_attempts INTEGER
  CHECK (maximum_provider_attempts >= 1),
ADD CONSTRAINT execution_budget_reservation_authority_is_complete CHECK (
  (
    authority_kind = 'legacy'
    AND configuration_revision_id IS NULL
    AND prompt_release_id IS NULL
    AND relevance_release_id IS NULL
    AND release_generation IS NULL
    AND search_queries IS NULL
    AND logical_model_calls_per_job IS NULL
    AND maximum_provider_attempts IS NULL
  ) OR (
    authority_kind = 'adopted'
    AND configuration_revision_id IS NOT NULL
    AND prompt_release_id IS NOT NULL
    AND relevance_release_id IS NOT NULL
    AND release_generation IS NULL
    AND search_queries IS NOT NULL
    AND logical_model_calls_per_job IS NOT NULL
    AND maximum_provider_attempts IS NOT NULL
  ) OR (
    authority_kind = 'pinned'
    AND configuration_revision_id IS NOT NULL
    AND prompt_release_id IS NOT NULL
    AND relevance_release_id IS NOT NULL
    AND release_generation IS NOT NULL
    AND search_queries IS NOT NULL
    AND logical_model_calls_per_job IS NOT NULL
    AND maximum_provider_attempts IS NOT NULL
  )
);

ALTER TABLE pipeline_runs
ADD CONSTRAINT pipeline_runs_budget_authority_unique UNIQUE (
  id, configuration_revision_id, prompt_release_id, relevance_release_id
);

ALTER TABLE execution_budget_reservations
ADD CONSTRAINT execution_budget_reservation_matches_run
FOREIGN KEY (
  pipeline_run_id, configuration_revision_id, prompt_release_id, relevance_release_id
)
REFERENCES pipeline_runs (
  id, configuration_revision_id, prompt_release_id, relevance_release_id
);

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
