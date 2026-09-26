CREATE TABLE qualification_evidence_executions (
  idempotency_key TEXT PRIMARY KEY CHECK (idempotency_key <> ''),
  target_id CHAR(64) NOT NULL REFERENCES qualification_targets(id),
  phase TEXT NOT NULL CHECK (
    phase IN ('input_preparation', 'relevance', 'enrichment', 'deduplication', 'composition')
  ),
  input_id CHAR(64) NOT NULL CHECK (input_id ~ '^[0-9a-f]{64}$'),
  artifact_id CHAR(64) NOT NULL REFERENCES implementation_artifacts(id),
  state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
  evidence_id CHAR(64) REFERENCES qualification_phase_evidence(id),
  failure TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  CHECK (
    (state = 'running' AND evidence_id IS NULL AND failure IS NULL AND finished_at IS NULL)
    OR (state = 'completed' AND evidence_id IS NOT NULL AND failure IS NULL AND finished_at IS NOT NULL)
    OR (state = 'failed' AND evidence_id IS NULL AND failure IS NOT NULL AND finished_at IS NOT NULL)
  )
);

CREATE OR REPLACE FUNCTION protect_qualification_evidence_execution()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'qualification evidence execution cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.state <> 'running'
     OR NEW.idempotency_key <> OLD.idempotency_key
     OR NEW.target_id <> OLD.target_id
     OR NEW.phase <> OLD.phase
     OR NEW.input_id <> OLD.input_id
     OR NEW.artifact_id <> OLD.artifact_id
     OR NEW.created_at <> OLD.created_at
     OR NEW.state NOT IN ('completed', 'failed') THEN
    RAISE EXCEPTION 'qualification evidence execution is immutable or terminal'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER qualification_evidence_execution_transitions
BEFORE UPDATE OR DELETE ON qualification_evidence_executions
FOR EACH ROW EXECUTE FUNCTION protect_qualification_evidence_execution();
