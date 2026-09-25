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
ADD CONSTRAINT onboarding_request_run_key UNIQUE (idempotency_key, run_id),
ADD COLUMN provider_attempt_count INTEGER NOT NULL DEFAULT 0
  CHECK (provider_attempt_count >= 0 AND provider_attempt_count <= max_provider_attempts);

CREATE OR REPLACE FUNCTION protect_onboarding_test_search_request()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'onboarding test search request cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.state IN ('completed', 'failed') THEN
    RAISE EXCEPTION 'onboarding test search request is terminal'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.idempotency_key <> OLD.idempotency_key
     OR NEW.run_id <> OLD.run_id
     OR NEW.configuration_revision_id <> OLD.configuration_revision_id
     OR NEW.prompt_release_id <> OLD.prompt_release_id
     OR NEW.relevance_release_id <> OLD.relevance_release_id
     OR NEW.release_generation <> OLD.release_generation
     OR NEW.budget_policy_version <> OLD.budget_policy_version
     OR NEW.budget_reservation_key <> OLD.budget_reservation_key
     OR NEW.max_queries <> OLD.max_queries
     OR NEW.max_urls <> OLD.max_urls
     OR NEW.max_jobs <> OLD.max_jobs
     OR NEW.max_work_attempts <> OLD.max_work_attempts
     OR NEW.max_provider_attempts <> OLD.max_provider_attempts
     OR NEW.run_allowance_usd <> OLD.run_allowance_usd
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'onboarding test search request authority is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.attempt_count < OLD.attempt_count
     OR NEW.provider_attempt_count < OLD.provider_attempt_count THEN
    RAISE EXCEPTION 'onboarding test search attempt count cannot decrease'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT (
    (OLD.state = 'pending' AND NEW.state = 'leased')
    OR (OLD.state = 'pending' AND NEW.state = 'failed')
    OR (OLD.state = 'leased' AND NEW.state = 'leased')
    OR (OLD.state = 'leased' AND NEW.state = 'completed')
    OR (OLD.state = 'leased' AND NEW.state = 'failed')
  ) THEN
    RAISE EXCEPTION 'invalid onboarding test search request transition'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TABLE onboarding_search_queries (
  request_key VARCHAR(200) NOT NULL
    REFERENCES onboarding_test_search_requests(idempotency_key),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  keyword TEXT NOT NULL CHECK (keyword <> ''),
  domain TEXT NOT NULL CHECK (domain <> ''),
  state TEXT NOT NULL CHECK (state IN ('reserved', 'completed', 'unavailable')),
  url_count INTEGER NOT NULL DEFAULT 0 CHECK (url_count >= 0),
  discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
  new_work_count INTEGER NOT NULL DEFAULT 0 CHECK (new_work_count >= 0),
  PRIMARY KEY (request_key, ordinal),
  CHECK (state <> 'reserved' OR (url_count = 0 AND discovered_count = 0 AND new_work_count = 0))
);

CREATE TABLE onboarding_provider_dispatches (
  request_key VARCHAR(200) NOT NULL
    REFERENCES onboarding_test_search_requests(idempotency_key),
  job_id UUID NOT NULL REFERENCES jobs(id),
  operation_key TEXT NOT NULL CHECK (operation_key <> ''),
  provider TEXT NOT NULL CHECK (provider IN ('openrouter', 'typesafe')),
  body_digest CHAR(64) NOT NULL,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
  state TEXT NOT NULL CHECK (state IN ('reserved', 'responded')),
  status_code INTEGER,
  response_body TEXT,
  provider_response_id TEXT,
  retry_after_seconds DOUBLE PRECISION,
  PRIMARY KEY (request_key, job_id, operation_key, provider, body_digest, attempt_number),
  CHECK (
    (state = 'reserved' AND status_code IS NULL AND response_body IS NULL)
    OR (state = 'responded' AND status_code IS NOT NULL AND response_body IS NOT NULL)
  )
);

CREATE FUNCTION guard_onboarding_provider_dispatch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' OR OLD.state = 'responded'
     OR NEW.request_key <> OLD.request_key OR NEW.job_id <> OLD.job_id
     OR NEW.provider <> OLD.provider OR NEW.body_digest <> OLD.body_digest
     OR NEW.operation_key <> OLD.operation_key
     OR NEW.attempt_number <> OLD.attempt_number OR NEW.state <> 'responded' THEN
    RAISE EXCEPTION 'onboarding provider dispatch is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER onboarding_provider_dispatches_are_guarded
BEFORE UPDATE OR DELETE ON onboarding_provider_dispatches
FOR EACH ROW EXECUTE FUNCTION guard_onboarding_provider_dispatch();

ALTER TABLE job_work_items
ADD COLUMN onboarding_request_key VARCHAR(200),
ADD CONSTRAINT onboarding_work_matches_request_run
FOREIGN KEY (onboarding_request_key, discovery_run_id)
REFERENCES onboarding_test_search_requests(idempotency_key, run_id);

CREATE INDEX claimable_onboarding_job_work_items
ON job_work_items (onboarding_request_key, COALESCE(retry_at, created_at), created_at, job_id)
WHERE onboarding_request_key IS NOT NULL AND state IN ('pending', 'failed', 'leased');
