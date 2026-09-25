ALTER TABLE pipeline_runs DROP CONSTRAINT pipeline_runs_kind_check;
ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_kind_check
CHECK (kind IN (
  'discovery', 'processing', 'reconcile', 'evaluation', 'orchestration',
  'reevaluation', 'onboarding'
));

ALTER TABLE pipeline_runs
DROP CONSTRAINT orchestration_runs_own_complete_release_target,
DROP CONSTRAINT orchestration_runs_own_configuration_revision,
ADD CONSTRAINT orchestration_runs_own_complete_release_target CHECK (
  kind NOT IN ('orchestration', 'reevaluation', 'onboarding')
  OR (prompt_release_id IS NOT NULL AND relevance_release_id IS NOT NULL)
) NOT VALID,
ADD CONSTRAINT orchestration_runs_own_configuration_revision CHECK (
  configuration_revision_id IS NULL
  OR kind IN ('orchestration', 'reevaluation', 'onboarding')
);

CREATE OR REPLACE FUNCTION require_complete_orchestration_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  checked_run_id UUID;
  run_kind TEXT;
  prompt_id CHAR(64);
  relevance_id CHAR(64);
  snapshot_count INTEGER;
BEGIN
  IF TG_TABLE_NAME = 'pipeline_runs' THEN
    checked_run_id := NEW.id;
  ELSE
    checked_run_id := COALESCE(NEW.pipeline_run_id, OLD.pipeline_run_id);
  END IF;
  SELECT kind, prompt_release_id, relevance_release_id
  INTO run_kind, prompt_id, relevance_id
  FROM pipeline_runs WHERE id = checked_run_id;
  IF run_kind IS NULL OR run_kind NOT IN ('orchestration', 'reevaluation', 'onboarding') THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO snapshot_count
  FROM run_exchange_rate_snapshots WHERE pipeline_run_id = checked_run_id;
  IF prompt_id IS NULL OR relevance_id IS NULL OR snapshot_count <> 1 THEN
    RAISE EXCEPTION '% run % requires one release target and rate snapshot',
      run_kind, checked_run_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

ALTER TABLE onboarding_test_search_requests
ADD CONSTRAINT onboarding_request_run_key UNIQUE (idempotency_key, run_id);

ALTER TABLE job_work_items
ADD COLUMN onboarding_request_key VARCHAR(200),
ADD CONSTRAINT onboarding_work_matches_request_run
FOREIGN KEY (onboarding_request_key, discovery_run_id)
REFERENCES onboarding_test_search_requests(idempotency_key, run_id);

CREATE INDEX claimable_onboarding_job_work_items
ON job_work_items (onboarding_request_key, COALESCE(retry_at, created_at), created_at, job_id)
WHERE onboarding_request_key IS NOT NULL AND state IN ('pending', 'failed', 'leased');
