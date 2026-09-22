CREATE TABLE execution_budget_policy (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  version BIGINT NOT NULL CHECK (version >= 1),
  monthly_limit_usd NUMERIC(18, 8) NOT NULL CHECK (monthly_limit_usd > 0),
  run_allowance_usd NUMERIC(18, 8) NOT NULL CHECK (
    run_allowance_usd > 0 AND run_allowance_usd <= monthly_limit_usd
  ),
  max_jobs_per_run INTEGER NOT NULL CHECK (max_jobs_per_run BETWEEN 1 AND 1000),
  max_search_queries_per_run INTEGER NOT NULL CHECK (max_search_queries_per_run >= 1),
  max_provider_attempts_per_run INTEGER NOT NULL CHECK (max_provider_attempts_per_run >= 1),
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by TEXT NOT NULL CHECK (updated_by <> '')
);

CREATE FUNCTION protect_execution_budget_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'execution budget policy cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION 'execution budget policy version must increment once'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER execution_budget_policy_is_versioned
BEFORE UPDATE OR DELETE ON execution_budget_policy
FOR EACH ROW EXECUTE FUNCTION protect_execution_budget_policy();

CREATE TABLE execution_budget_reservations (
  idempotency_key TEXT PRIMARY KEY CHECK (idempotency_key <> ''),
  policy_version BIGINT NOT NULL CHECK (policy_version >= 1),
  period_start DATE NOT NULL,
  reserved_usd NUMERIC(18, 8) NOT NULL CHECK (reserved_usd > 0),
  consumed_usd NUMERIC(18, 8) CHECK (
    consumed_usd >= 0 AND consumed_usd <= reserved_usd
  ),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'settled')),
  max_jobs INTEGER NOT NULL CHECK (max_jobs >= 1),
  jobs_reserved INTEGER NOT NULL DEFAULT 0 CHECK (
    jobs_reserved >= 0 AND jobs_reserved <= max_jobs
  ),
  discovery_reserved BOOLEAN NOT NULL DEFAULT FALSE,
  pipeline_run_id UUID REFERENCES pipeline_runs(id),
  created_at TIMESTAMPTZ NOT NULL,
  settled_at TIMESTAMPTZ,
  CHECK (
    (status = 'reserved' AND consumed_usd IS NULL AND settled_at IS NULL)
    OR (status = 'settled' AND consumed_usd IS NOT NULL AND settled_at IS NOT NULL)
  )
);

CREATE FUNCTION protect_execution_budget_reservation()
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

CREATE TRIGGER execution_budget_reservations_are_append_only
BEFORE UPDATE OR DELETE ON execution_budget_reservations
FOR EACH ROW EXECUTE FUNCTION protect_execution_budget_reservation();
