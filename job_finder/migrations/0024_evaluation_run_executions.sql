ALTER TABLE evaluation_runs
ADD CONSTRAINT evaluation_runs_exact_execution_key UNIQUE (
  id, manifest_id, prompt_release_id, relevance_release_id,
  idempotency_key, implementation_ref
);

CREATE FUNCTION valid_evaluation_timestamp(candidate TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
  IF candidate !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$' THEN
    RETURN FALSE;
  END IF;
  PERFORM candidate::TIMESTAMPTZ;
  RETURN TRUE;
EXCEPTION WHEN OTHERS THEN
  RETURN FALSE;
END;
$$;

CREATE FUNCTION valid_evaluation_exchange_rate_snapshot(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  SELECT jsonb_typeof(candidate) = 'object'
    AND candidate ?& ARRAY['rates', 'source', 'observed_at']
    AND candidate - ARRAY['rates', 'source', 'observed_at'] = '{}'::jsonb
    AND jsonb_typeof(candidate -> 'rates') = 'object'
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_each(candidate -> 'rates') AS rate(currency, value)
      WHERE jsonb_typeof(value) NOT IN ('number', 'string')
        OR value #>> '{}' !~ '^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$'
    )
    AND candidate ->> 'source' IN ('frankfurter', 'fallback')
    AND jsonb_typeof(candidate -> 'observed_at') = 'string'
    AND valid_evaluation_timestamp(candidate ->> 'observed_at');
$$;

CREATE TABLE evaluation_run_executions (
  id CHAR(64) PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  manifest_id CHAR(64) NOT NULL REFERENCES evaluation_manifests(id),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
  implementation_ref TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
  exchange_rate_snapshot JSONB,
  exchange_rate_digest CHAR(64),
  request_count INTEGER CHECK (request_count >= 0),
  input_tokens BIGINT CHECK (input_tokens >= 0),
  output_tokens BIGINT CHECK (output_tokens >= 0),
  cost_usd NUMERIC CHECK (cost_usd >= 0),
  usage_complete BOOLEAN,
  p50_latency_ms NUMERIC CHECK (p50_latency_ms >= 0),
  p95_latency_ms NUMERIC CHECK (p95_latency_ms >= 0),
  run_id CHAR(64) UNIQUE REFERENCES evaluation_runs(id),
  failure JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  terminal_at TIMESTAMPTZ,
  CONSTRAINT evaluation_execution_rate_snapshot_shape CHECK (
    exchange_rate_snapshot IS NULL
    OR valid_evaluation_exchange_rate_snapshot(exchange_rate_snapshot)
  ),
  CONSTRAINT evaluation_execution_rate_digest_matches CHECK (
    exchange_rate_snapshot IS NULL
    OR exchange_rate_digest = encode(
      sha256(convert_to(canonical_job_finder_json(exchange_rate_snapshot), 'UTF8')),
      'hex'
    )
  ),
  CONSTRAINT evaluation_execution_latency_presence_matches_requests CHECK (
    request_count IS NULL
    OR (request_count = 0
      AND p50_latency_ms IS NULL
      AND p95_latency_ms IS NULL)
    OR (request_count > 0
      AND p50_latency_ms IS NOT NULL
      AND p95_latency_ms IS NOT NULL)
  ),
  CONSTRAINT evaluation_execution_latency_percentiles_ordered CHECK (
    p50_latency_ms IS NULL OR p50_latency_ms <= p95_latency_ms
  ),
  CONSTRAINT evaluation_execution_terminal_time_ordered CHECK (
    terminal_at IS NULL OR terminal_at >= created_at
  ),
  CONSTRAINT evaluation_execution_failure_shape CHECK (
    failure IS NULL
    OR (jsonb_typeof(failure) = 'object'
      AND failure ?& ARRAY['code', 'message']
      AND failure - ARRAY['code', 'message', 'error_type'] = '{}'::jsonb
      AND jsonb_typeof(failure -> 'code') = 'string'
      AND length(failure ->> 'code') > 0
      AND jsonb_typeof(failure -> 'message') = 'string'
      AND length(failure ->> 'message') > 0
      AND (NOT failure ? 'error_type'
        OR jsonb_typeof(failure -> 'error_type') = 'string'))
  ),
  CONSTRAINT evaluation_execution_state_columns CHECK (
    (state = 'running'
      AND exchange_rate_snapshot IS NOT NULL
      AND exchange_rate_digest IS NOT NULL
      AND relevance_release_id IS NOT NULL
      AND request_count IS NULL
      AND input_tokens IS NULL
      AND output_tokens IS NULL
      AND cost_usd IS NULL
      AND usage_complete IS NULL
      AND p50_latency_ms IS NULL
      AND p95_latency_ms IS NULL
      AND run_id IS NULL
      AND failure IS NULL
      AND terminal_at IS NULL)
    OR (state = 'completed'
      AND run_id IS NOT NULL
      AND failure IS NULL
      AND terminal_at IS NOT NULL
      AND (
        (exchange_rate_snapshot IS NOT NULL
          AND exchange_rate_digest IS NOT NULL
          AND relevance_release_id IS NOT NULL
          AND request_count IS NOT NULL
          AND input_tokens IS NOT NULL
          AND output_tokens IS NOT NULL
          AND cost_usd IS NOT NULL
          AND usage_complete IS NOT NULL)
        OR (exchange_rate_snapshot IS NULL
          AND exchange_rate_digest IS NULL
          AND request_count IS NULL
          AND input_tokens IS NULL
          AND output_tokens IS NULL
          AND cost_usd IS NULL
          AND usage_complete IS NULL
          AND p50_latency_ms IS NULL
          AND p95_latency_ms IS NULL)
      ))
    OR (state = 'failed'
      AND exchange_rate_snapshot IS NOT NULL
      AND exchange_rate_digest IS NOT NULL
      AND relevance_release_id IS NOT NULL
      AND run_id IS NULL
      AND failure IS NOT NULL
      AND jsonb_typeof(failure) = 'object'
      AND terminal_at IS NOT NULL
      AND (
        (request_count IS NULL
          AND input_tokens IS NULL
          AND output_tokens IS NULL
          AND cost_usd IS NULL
          AND usage_complete IS NULL
          AND p50_latency_ms IS NULL
          AND p95_latency_ms IS NULL)
        OR (request_count IS NOT NULL
          AND input_tokens IS NOT NULL
          AND output_tokens IS NOT NULL
          AND cost_usd IS NOT NULL
          AND usage_complete IS NOT NULL)
      ))
  ),
  CONSTRAINT evaluation_execution_exact_completed_run FOREIGN KEY (
    run_id, manifest_id, prompt_release_id, relevance_release_id,
    idempotency_key, implementation_ref
  ) REFERENCES evaluation_runs (
    id, manifest_id, prompt_release_id, relevance_release_id,
    idempotency_key, implementation_ref
  ) MATCH SIMPLE
);

INSERT INTO evaluation_run_executions (
  id, idempotency_key, manifest_id, prompt_release_id, relevance_release_id,
  implementation_ref, state, run_id, created_at, terminal_at
)
SELECT encode(
         sha256(convert_to('evaluation_execution:' || idempotency_key, 'UTF8')),
         'hex'
       ),
       idempotency_key, manifest_id, prompt_release_id, relevance_release_id,
       implementation_ref, 'completed', id, completed_at, completed_at
FROM evaluation_runs;

CREATE FUNCTION require_running_evaluation_execution_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.state <> 'running' THEN
    RAISE EXCEPTION 'new evaluation run executions must start running'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER evaluation_execution_inserts_start_running
BEFORE INSERT ON evaluation_run_executions
FOR EACH ROW EXECUTE FUNCTION require_running_evaluation_execution_insert();

CREATE FUNCTION enforce_evaluation_execution_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'evaluation run executions cannot be deleted'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF OLD.state <> 'running' THEN
    RAISE EXCEPTION 'terminal evaluation run executions are immutable'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.state NOT IN ('completed', 'failed') THEN
    RAISE EXCEPTION 'running evaluation execution must become completed or failed'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF (NEW.id, NEW.idempotency_key, NEW.manifest_id, NEW.prompt_release_id,
      NEW.relevance_release_id, NEW.implementation_ref,
      NEW.exchange_rate_snapshot, NEW.exchange_rate_digest, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.idempotency_key, OLD.manifest_id, OLD.prompt_release_id,
      OLD.relevance_release_id, OLD.implementation_ref,
      OLD.exchange_rate_snapshot, OLD.exchange_rate_digest, OLD.created_at) THEN
    RAISE EXCEPTION 'evaluation execution command and rate identity are immutable'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER evaluation_execution_transitions_are_constrained
BEFORE UPDATE OR DELETE ON evaluation_run_executions
FOR EACH ROW EXECUTE FUNCTION enforce_evaluation_execution_transition();
