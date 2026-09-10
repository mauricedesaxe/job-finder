CREATE FUNCTION reject_immutable_job_finder_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
    USING ERRCODE = 'check_violation';
END;
$$;

CREATE TABLE pipeline_runs (
  id UUID PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('discovery', 'processing', 'reconcile', 'evaluation')),
  implementation_ref TEXT NOT NULL,
  prompt_release_id CHAR(64),
  parameters JSONB NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  error JSONB,
  UNIQUE (id, prompt_release_id),
  CHECK ((status = 'running') = (completed_at IS NULL)),
  CHECK ((status = 'failed') = (error IS NOT NULL))
);

CREATE TABLE jobs (
  id UUID PRIMARY KEY,
  raw_url TEXT NOT NULL UNIQUE,
  first_discovered_at TIMESTAMPTZ NOT NULL,
  last_discovered_at TIMESTAMPTZ NOT NULL,
  UNIQUE (id, raw_url),
  CHECK (first_discovered_at <= last_discovered_at)
);

CREATE TABLE job_snapshots (
  id CHAR(64) PRIMARY KEY,
  job_id UUID NOT NULL,
  content_digest CHAR(64) NOT NULL,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  normalized_company TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  source TEXT NOT NULL,
  raw_url TEXT NOT NULL,
  description TEXT NOT NULL,
  location TEXT NOT NULL,
  keywords JSONB NOT NULL,
  date_posted DATE,
  observed_at TIMESTAMPTZ NOT NULL,
  ats_evidence JSONB,
  UNIQUE (job_id, content_digest),
  FOREIGN KEY (job_id, raw_url) REFERENCES jobs(id, raw_url),
  CHECK (jsonb_typeof(keywords) = 'array')
);

CREATE TRIGGER job_snapshots_are_immutable
BEFORE UPDATE OR DELETE ON job_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE processing_attempts (
  id UUID PRIMARY KEY,
  pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
  job_id UUID REFERENCES jobs(id),
  operation_key TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
  input_digest CHAR(64) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  error JSONB,
  UNIQUE NULLS NOT DISTINCT (pipeline_run_id, job_id, operation_key, attempt_number),
  UNIQUE (id, pipeline_run_id),
  UNIQUE (id, pipeline_run_id, operation_key, input_digest),
  CHECK ((status = 'running') = (completed_at IS NULL)),
  CHECK ((status = 'failed') = (error IS NOT NULL))
);

CREATE TABLE pipeline_receipts (
  id CHAR(64) PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
  job_id UUID REFERENCES jobs(id),
  operation_key TEXT NOT NULL,
  input_digest CHAR(64) NOT NULL,
  output_digest CHAR(64) NOT NULL,
  output JSONB NOT NULL,
  implementation_ref TEXT NOT NULL,
  prompt_release_id CHAR(64),
  completed_at TIMESTAMPTZ NOT NULL
);

CREATE TRIGGER pipeline_receipts_are_immutable
BEFORE UPDATE OR DELETE ON pipeline_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE prompt_versions (
  id CHAR(64) PRIMARY KEY,
  prompt_name TEXT NOT NULL,
  content_digest CHAR(64) NOT NULL,
  messages JSONB NOT NULL,
  input_schema JSONB NOT NULL,
  output_schema JSONB NOT NULL,
  model TEXT NOT NULL,
  parameters JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (prompt_name, content_digest),
  UNIQUE (prompt_name, id),
  CHECK (jsonb_typeof(messages) = 'array'),
  CHECK (jsonb_typeof(input_schema) = 'object'),
  CHECK (jsonb_typeof(output_schema) = 'object'),
  CHECK (jsonb_typeof(parameters) = 'object')
);

CREATE TABLE prompt_releases (
  id CHAR(64) PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  content_digest CHAR(64) NOT NULL UNIQUE,
  expected_member_count INTEGER NOT NULL CHECK (expected_member_count > 0),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL
);

CREATE TABLE prompt_release_members (
  release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  prompt_name TEXT NOT NULL,
  prompt_version_id CHAR(64) NOT NULL,
  PRIMARY KEY (release_id, prompt_name),
  UNIQUE (release_id, prompt_name, prompt_version_id),
  FOREIGN KEY (prompt_name, prompt_version_id)
    REFERENCES prompt_versions(prompt_name, id)
);

CREATE FUNCTION require_complete_prompt_release()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  checked_release_id CHAR(64);
  expected_count INTEGER;
  actual_count INTEGER;
BEGIN
  IF TG_TABLE_NAME = 'prompt_releases' THEN
    checked_release_id := NEW.id;
  ELSE
    checked_release_id := COALESCE(NEW.release_id, OLD.release_id);
  END IF;

  SELECT expected_member_count INTO expected_count
  FROM prompt_releases
  WHERE id = checked_release_id;

  IF expected_count IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT count(*) INTO actual_count
  FROM prompt_release_members
  WHERE release_id = checked_release_id;

  IF actual_count <> expected_count THEN
    RAISE EXCEPTION 'prompt release % requires % members, found %',
      checked_release_id, expected_count, actual_count
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER prompt_releases_are_complete
AFTER INSERT OR UPDATE ON prompt_releases
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_prompt_release();

CREATE CONSTRAINT TRIGGER prompt_release_members_keep_release_complete
AFTER INSERT OR UPDATE OR DELETE ON prompt_release_members
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_prompt_release();

ALTER TABLE pipeline_runs
ADD CONSTRAINT pipeline_runs_prompt_release
FOREIGN KEY (prompt_release_id) REFERENCES prompt_releases(id);

CREATE TRIGGER prompt_versions_are_immutable
BEFORE UPDATE OR DELETE ON prompt_versions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TRIGGER prompt_releases_are_immutable
BEFORE UPDATE OR DELETE ON prompt_releases
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TRIGGER prompt_release_members_are_immutable
BEFORE UPDATE OR DELETE ON prompt_release_members
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE model_call_attempts (
  id UUID PRIMARY KEY,
  processing_attempt_id UUID NOT NULL,
  pipeline_run_id UUID NOT NULL,
  prompt_release_id CHAR(64) NOT NULL,
  request_id CHAR(64) NOT NULL,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
  operation_key TEXT NOT NULL,
  prompt_name TEXT NOT NULL,
  prompt_version_id CHAR(64) NOT NULL,
  input_digest CHAR(64) NOT NULL,
  requested_model TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider = 'openrouter'),
  provider_response_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'retryable_error', 'terminal_error')),
  parsed_output JSONB,
  raw_response JSONB,
  input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
  output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
  cost_usd NUMERIC(18, 8) CHECK (cost_usd IS NULL OR cost_usd >= 0),
  latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
  error JSONB,
  observed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (request_id, attempt_number),
  FOREIGN KEY (processing_attempt_id, pipeline_run_id, operation_key, input_digest)
    REFERENCES processing_attempts(id, pipeline_run_id, operation_key, input_digest),
  FOREIGN KEY (pipeline_run_id, prompt_release_id)
    REFERENCES pipeline_runs(id, prompt_release_id),
  FOREIGN KEY (prompt_release_id, prompt_name, prompt_version_id)
    REFERENCES prompt_release_members(release_id, prompt_name, prompt_version_id),
  CHECK ((status = 'accepted') = (parsed_output IS NOT NULL)),
  CHECK ((status = 'accepted') = (error IS NULL))
);

CREATE UNIQUE INDEX one_accepted_model_attempt_per_request
ON model_call_attempts(request_id)
WHERE status = 'accepted';

CREATE TRIGGER model_call_attempts_are_immutable
BEFORE UPDATE OR DELETE ON model_call_attempts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE evaluation_decisions (
  id CHAR(64) PRIMARY KEY,
  snapshot_id CHAR(64) NOT NULL REFERENCES job_snapshots(id),
  pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  policy_version TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (
    outcome IN ('qualified', 'rejected', 'duplicate', 'company_blocked', 'company_applied')
  ),
  matched_profile TEXT,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (snapshot_id, prompt_release_id, policy_version),
  FOREIGN KEY (pipeline_run_id, prompt_release_id)
    REFERENCES pipeline_runs(id, prompt_release_id),
  CHECK ((outcome = 'qualified') = (matched_profile IS NOT NULL))
);

CREATE TRIGGER evaluation_decisions_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE review_items (
  id UUID PRIMARY KEY,
  evaluation_id CHAR(64) NOT NULL UNIQUE REFERENCES evaluation_decisions(id),
  review_day DATE NOT NULL,
  lane TEXT NOT NULL CHECK (lane IN ('qualified', 'rejected_audit')),
  position INTEGER NOT NULL CHECK (position >= 0),
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (review_day, lane, position)
);

CREATE TRIGGER review_items_are_immutable
BEFORE UPDATE OR DELETE ON review_items
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE review_events (
  id UUID PRIMARY KEY,
  review_item_id UUID NOT NULL REFERENCES review_items(id),
  decision TEXT NOT NULL CHECK (decision IN ('pursue', 'reject', 'unsure')),
  target_profile TEXT,
  primary_reason TEXT NOT NULL,
  note TEXT,
  block_company BOOLEAN NOT NULL,
  actor TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  CHECK (length(COALESCE(note, '')) <= 2000)
);

CREATE TRIGGER review_events_are_immutable
BEFORE UPDATE OR DELETE ON review_events
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE company_policies (
  normalized_company TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  policy TEXT NOT NULL CHECK (policy IN ('blocked', 'recent_application')),
  source_review_event_id UUID REFERENCES review_events(id),
  effective_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  CHECK (expires_at IS NULL OR effective_at < expires_at)
);

CREATE TABLE application_events (
  id UUID PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES jobs(id),
  kind TEXT NOT NULL CHECK (kind IN ('planned', 'applied', 'skipped', 'withdrawn')),
  source_review_event_id UUID REFERENCES review_events(id),
  actor TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TRIGGER application_events_are_immutable
BEFORE UPDATE OR DELETE ON application_events
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE langfuse_projection_items (
  id CHAR(64) PRIMARY KEY,
  kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  payload_digest CHAR(64) NOT NULL,
  payload JSONB NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'completed', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  owner_token UUID,
  lease_expires_at TIMESTAMPTZ,
  retry_at TIMESTAMPTZ,
  remote_id TEXT,
  last_error JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  UNIQUE (kind, source_id),
  CHECK ((state = 'leased') = (owner_token IS NOT NULL)),
  CHECK ((state = 'leased') = (lease_expires_at IS NOT NULL)),
  CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);
