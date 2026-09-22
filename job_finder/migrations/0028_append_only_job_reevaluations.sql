ALTER TABLE pipeline_runs DROP CONSTRAINT pipeline_runs_kind_check;
ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_kind_check
CHECK (kind IN (
  'discovery', 'processing', 'reconcile', 'evaluation', 'orchestration', 'reevaluation'
));

ALTER TABLE pipeline_runs
DROP CONSTRAINT orchestration_runs_own_complete_release_target,
DROP CONSTRAINT orchestration_runs_own_configuration_revision,
ADD CONSTRAINT orchestration_runs_own_complete_release_target CHECK (
  kind NOT IN ('orchestration', 'reevaluation')
  OR (prompt_release_id IS NOT NULL AND relevance_release_id IS NOT NULL)
) NOT VALID,
ADD CONSTRAINT orchestration_runs_own_configuration_revision CHECK (
  configuration_revision_id IS NULL OR kind IN ('orchestration', 'reevaluation')
),
ADD CONSTRAINT pipeline_runs_exact_release_target
UNIQUE (id, prompt_release_id, relevance_release_id);

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
  IF run_kind IS NULL OR run_kind NOT IN ('orchestration', 'reevaluation') THEN
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

ALTER TABLE evaluation_decisions
ADD CONSTRAINT evaluation_decisions_id_snapshot_key UNIQUE (id, snapshot_id),
ADD CONSTRAINT evaluation_decisions_source_provenance_key
UNIQUE (id, snapshot_id, pipeline_run_id);

ALTER TABLE job_snapshots
ADD CONSTRAINT job_snapshots_id_job_key UNIQUE (id, job_id);

CREATE TABLE job_reevaluation_requests (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  expected_decision_id CHAR(64) NOT NULL,
  expected_snapshot_id CHAR(64) NOT NULL,
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  outcome TEXT NOT NULL CHECK (
    outcome IN ('accepted', 'not_found', 'source_changed', 'active_work', 'unsupported')
  ),
  job_id UUID,
  source_decision_id CHAR(64),
  source_snapshot_id CHAR(64),
  source_pipeline_run_id UUID REFERENCES pipeline_runs(id),
  reevaluation_pipeline_run_id UUID UNIQUE REFERENCES pipeline_runs(id),
  prompt_release_id CHAR(64) REFERENCES prompt_releases(id),
  relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
  release_generation BIGINT CHECK (release_generation >= 0),
  observed_work_state TEXT CHECK (
    observed_work_state IN ('pending', 'leased', 'failed', 'completed', 'terminal_error')
  ),
  conflict_code TEXT,
  conflict_reason TEXT,
  UNIQUE (
    idempotency_key, source_decision_id, source_snapshot_id,
    reevaluation_pipeline_run_id, prompt_release_id, relevance_release_id
  ),
  UNIQUE (idempotency_key, job_id),
  FOREIGN KEY (source_snapshot_id, job_id) REFERENCES job_snapshots(id, job_id),
  FOREIGN KEY (source_decision_id, source_snapshot_id, source_pipeline_run_id)
    REFERENCES evaluation_decisions(id, snapshot_id, pipeline_run_id),
  FOREIGN KEY (
    reevaluation_pipeline_run_id, prompt_release_id, relevance_release_id
  ) REFERENCES pipeline_runs(id, prompt_release_id, relevance_release_id),
  CONSTRAINT job_reevaluation_request_shape CHECK (
    (
      outcome = 'accepted'
      AND job_id IS NOT NULL
      AND source_decision_id IS NOT NULL
      AND source_snapshot_id IS NOT NULL
      AND source_decision_id = expected_decision_id
      AND source_snapshot_id = expected_snapshot_id
      AND source_pipeline_run_id IS NOT NULL
      AND reevaluation_pipeline_run_id IS NOT NULL
      AND prompt_release_id IS NOT NULL
      AND relevance_release_id IS NOT NULL
      AND release_generation IS NOT NULL
      AND observed_work_state = 'completed'
      AND conflict_code IS NULL
      AND conflict_reason IS NULL
    ) OR (
      outcome <> 'accepted'
      AND source_decision_id IS NULL
      AND source_snapshot_id IS NULL
      AND source_pipeline_run_id IS NULL
      AND reevaluation_pipeline_run_id IS NULL
      AND prompt_release_id IS NULL
      AND relevance_release_id IS NULL
      AND release_generation IS NULL
      AND conflict_code IS NOT NULL
      AND conflict_reason IS NOT NULL
    )
  )
);

CREATE TRIGGER job_reevaluation_requests_are_immutable
BEFORE UPDATE OR DELETE ON job_reevaluation_requests
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

ALTER TABLE job_work_items
ADD COLUMN active_reevaluation_key VARCHAR(200)
REFERENCES job_reevaluation_requests(idempotency_key),
ADD CONSTRAINT job_work_items_reevaluation_job
FOREIGN KEY (active_reevaluation_key, job_id)
REFERENCES job_reevaluation_requests(idempotency_key, job_id);

ALTER TABLE evaluation_decisions
ADD COLUMN relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
ADD COLUMN source_snapshot_id CHAR(64) REFERENCES job_snapshots(id),
ADD COLUMN predecessor_decision_id CHAR(64) REFERENCES evaluation_decisions(id),
ADD COLUMN reevaluation_request_key VARCHAR(200)
  REFERENCES job_reevaluation_requests(idempotency_key),
ADD CONSTRAINT reevaluation_decision_provenance CHECK (
  (reevaluation_request_key IS NULL
    AND source_snapshot_id IS NULL
    AND predecessor_decision_id IS NULL)
  OR (reevaluation_request_key IS NOT NULL
    AND source_snapshot_id IS NOT NULL
    AND predecessor_decision_id IS NOT NULL
    AND relevance_release_id IS NOT NULL)
),
ADD CONSTRAINT evaluation_decisions_exact_release_target
FOREIGN KEY (pipeline_run_id, prompt_release_id, relevance_release_id)
REFERENCES pipeline_runs(id, prompt_release_id, relevance_release_id),
ADD CONSTRAINT evaluation_decisions_exact_reevaluation_request
FOREIGN KEY (
  reevaluation_request_key, predecessor_decision_id, source_snapshot_id,
  pipeline_run_id, prompt_release_id, relevance_release_id
) REFERENCES job_reevaluation_requests (
  idempotency_key, source_decision_id, source_snapshot_id,
  reevaluation_pipeline_run_id, prompt_release_id, relevance_release_id
);

ALTER TABLE evaluation_decisions
DROP CONSTRAINT evaluation_decisions_snapshot_id_prompt_release_id_policy_v_key;

CREATE UNIQUE INDEX one_ordinary_decision_per_input_target
ON evaluation_decisions (
  snapshot_id, prompt_release_id, relevance_release_id, policy_version
) NULLS NOT DISTINCT
WHERE reevaluation_request_key IS NULL;

CREATE UNIQUE INDEX one_decision_per_reevaluation_request
ON evaluation_decisions (reevaluation_request_key)
WHERE reevaluation_request_key IS NOT NULL;

CREATE FUNCTION lock_snapshot_reevaluation_changes()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(
    hashtextextended(
      'job_reevaluation_snapshot:' || COALESCE(NEW.snapshot_id, OLD.snapshot_id),
      0
    )
  );
  RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE TRIGGER snapshot_corrections_serialize_with_reevaluation
BEFORE INSERT OR UPDATE OR DELETE ON snapshot_corrections
FOR EACH ROW EXECUTE FUNCTION lock_snapshot_reevaluation_changes();

CREATE FUNCTION protect_reevaluation_run_provenance()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'reevaluation run provenance is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF ROW(
    NEW.id, NEW.idempotency_key, NEW.kind, NEW.implementation_ref,
    NEW.configuration_revision_id, NEW.prompt_release_id,
    NEW.relevance_release_id, NEW.parameters, NEW.started_at
  ) IS DISTINCT FROM ROW(
    OLD.id, OLD.idempotency_key, OLD.kind, OLD.implementation_ref,
    OLD.configuration_revision_id, OLD.prompt_release_id,
    OLD.relevance_release_id, OLD.parameters, OLD.started_at
  ) THEN
    RAISE EXCEPTION 'reevaluation run provenance is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER reevaluation_run_provenance_is_immutable
BEFORE UPDATE OR DELETE ON pipeline_runs
FOR EACH ROW
WHEN (OLD.kind = 'reevaluation')
EXECUTE FUNCTION protect_reevaluation_run_provenance();

CREATE FUNCTION require_reevaluation_decision_job_match()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.reevaluation_request_key IS NULL THEN
    RETURN NULL;
  END IF;
  PERFORM 1
  FROM job_reevaluation_requests request
  JOIN job_snapshots output_snapshot ON output_snapshot.id = NEW.snapshot_id
  WHERE request.idempotency_key = NEW.reevaluation_request_key
    AND request.job_id = output_snapshot.job_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'reevaluation decision output must belong to the requested job'
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER reevaluation_decisions_match_requested_job
AFTER INSERT OR UPDATE OF snapshot_id, reevaluation_request_key ON evaluation_decisions
DEFERRABLE INITIALLY IMMEDIATE
FOR EACH ROW EXECUTE FUNCTION require_reevaluation_decision_job_match();
