ALTER TABLE pipeline_runs DROP CONSTRAINT pipeline_runs_kind_check;
ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_kind_check
CHECK (kind IN ('discovery', 'processing', 'reconcile', 'evaluation', 'orchestration'));

ALTER TABLE evaluation_decisions ADD COLUMN decision_stage TEXT;
UPDATE evaluation_decisions
SET decision_stage = CASE WHEN outcome = 'rejected' THEN 'evaluation' ELSE 'qualified' END;
ALTER TABLE evaluation_decisions
ALTER COLUMN decision_stage SET NOT NULL,
ALTER COLUMN decision_stage SET DEFAULT 'qualified',
ADD CONSTRAINT evaluation_decisions_stage_check
CHECK (decision_stage IN ('ats_structural', 'structural', 'evaluation', 'qualified'));

CREATE TABLE run_exchange_rate_snapshots (
  pipeline_run_id UUID PRIMARY KEY REFERENCES pipeline_runs(id),
  content_digest CHAR(64) NOT NULL,
  rates JSONB NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('frankfurter', 'fallback')),
  observed_at TIMESTAMPTZ NOT NULL,
  CHECK (jsonb_typeof(rates) = 'object')
);

CREATE TRIGGER run_exchange_rate_snapshots_are_immutable
BEFORE UPDATE OR DELETE ON run_exchange_rate_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_complete_orchestration_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  checked_run_id UUID;
  run_kind TEXT;
  release_id CHAR(64);
  snapshot_count INTEGER;
BEGIN
  IF TG_TABLE_NAME = 'pipeline_runs' THEN
    checked_run_id := NEW.id;
  ELSE
    checked_run_id := COALESCE(NEW.pipeline_run_id, OLD.pipeline_run_id);
  END IF;
  SELECT kind, prompt_release_id INTO run_kind, release_id
  FROM pipeline_runs WHERE id = checked_run_id;
  IF run_kind IS NULL OR run_kind <> 'orchestration' THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO snapshot_count
  FROM run_exchange_rate_snapshots WHERE pipeline_run_id = checked_run_id;
  IF release_id IS NULL OR snapshot_count <> 1 THEN
    RAISE EXCEPTION 'orchestration run % requires one prompt release and rate snapshot',
      checked_run_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER orchestration_runs_are_complete
AFTER INSERT OR UPDATE ON pipeline_runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_orchestration_run();

CREATE CONSTRAINT TRIGGER rate_snapshots_keep_orchestration_runs_complete
AFTER INSERT OR UPDATE OR DELETE ON run_exchange_rate_snapshots
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_orchestration_run();

CREATE TABLE job_discoveries (
  pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
  job_id UUID NOT NULL REFERENCES jobs(id),
  keyword TEXT NOT NULL,
  domain TEXT NOT NULL,
  discovered_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (pipeline_run_id, job_id)
);

CREATE TRIGGER job_discoveries_are_immutable
BEFORE UPDATE OR DELETE ON job_discoveries
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE job_work_items (
  job_id UUID PRIMARY KEY REFERENCES jobs(id),
  discovery_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
  keyword TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('pending', 'leased', 'failed', 'completed', 'terminal_error')
  ),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  owner_token UUID,
  lease_expires_at TIMESTAMPTZ,
  retry_at TIMESTAMPTZ,
  terminal_decision_id CHAR(64) REFERENCES evaluation_decisions(id),
  last_error JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  CHECK ((state = 'leased') = (owner_token IS NOT NULL)),
  CHECK ((state = 'leased') = (lease_expires_at IS NOT NULL)),
  CHECK ((state = 'completed') = (terminal_decision_id IS NOT NULL)),
  CHECK ((state IN ('completed', 'terminal_error')) = (completed_at IS NOT NULL)),
  CHECK ((state IN ('failed', 'terminal_error')) = (last_error IS NOT NULL)),
  CHECK ((state = 'failed') = (retry_at IS NOT NULL)),
  CHECK (
    last_error IS NULL
    OR (
      jsonb_typeof(last_error) = 'object'
      AND last_error ? 'retryability'
      AND last_error->>'retryability' IN ('retryable', 'terminal')
      AND last_error ? 'code'
      AND last_error ? 'reason'
    )
  )
);

CREATE INDEX claimable_job_work_items
ON job_work_items (COALESCE(retry_at, created_at), created_at, job_id)
WHERE state IN ('pending', 'failed', 'leased');

CREATE UNIQUE INDEX one_processing_input_per_run_job_operation
ON processing_attempts (pipeline_run_id, job_id, operation_key, input_digest)
NULLS NOT DISTINCT;


ALTER TABLE processing_attempts
ADD CONSTRAINT processing_attempts_failure_shape
CHECK (
  status <> 'failed'
  OR (
    jsonb_typeof(error) = 'object'
    AND error ? 'retryability'
    AND error->>'retryability' IN ('retryable', 'terminal')
    AND error ? 'code'
    AND error ? 'reason'
  )
);
