ALTER TABLE prompt_promotion_decisions
ADD CONSTRAINT prompt_promotion_decisions_exact_approval_key UNIQUE (
  id, decision,
  baseline_prompt_release_id, baseline_relevance_release_id,
  candidate_prompt_release_id, candidate_relevance_release_id
);

CREATE TABLE active_release_target (
  singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  relevance_release_id CHAR(64) NOT NULL REFERENCES relevance_releases(id),
  generation BIGINT NOT NULL CHECK (generation >= 0),
  activated_at TIMESTAMPTZ NOT NULL,
  activated_by VARCHAR(200) NOT NULL CHECK (activated_by <> '')
);

CREATE TABLE release_target_activation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  outcome TEXT NOT NULL CHECK (outcome IN ('activated', 'active_changed')),
  promotion_decision_id CHAR(64) NOT NULL,
  promotion_decision TEXT NOT NULL DEFAULT 'approved' CHECK (promotion_decision = 'approved'),
  baseline_prompt_release_id CHAR(64) NOT NULL,
  baseline_relevance_release_id CHAR(64) NOT NULL,
  candidate_prompt_release_id CHAR(64) NOT NULL,
  candidate_relevance_release_id CHAR(64) NOT NULL,
  expected_prompt_release_id CHAR(64) NOT NULL,
  expected_relevance_release_id CHAR(64) NOT NULL,
  expected_generation BIGINT NOT NULL CHECK (expected_generation >= 0),
  observed_prompt_release_id CHAR(64) NOT NULL,
  observed_relevance_release_id CHAR(64) NOT NULL,
  observed_generation BIGINT NOT NULL CHECK (observed_generation >= 0),
  observed_activated_at TIMESTAMPTZ NOT NULL,
  observed_activated_by VARCHAR(200) NOT NULL CHECK (observed_activated_by <> ''),
  resulting_generation BIGINT NOT NULL CHECK (resulting_generation >= 0),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (
    promotion_decision_id, promotion_decision,
    baseline_prompt_release_id, baseline_relevance_release_id,
    candidate_prompt_release_id, candidate_relevance_release_id
  ) REFERENCES prompt_promotion_decisions (
    id, decision,
    baseline_prompt_release_id, baseline_relevance_release_id,
    candidate_prompt_release_id, candidate_relevance_release_id
  ),
  CONSTRAINT release_target_activation_receipt_shape CHECK (
    (
      outcome = 'activated'
      AND (expected_prompt_release_id, expected_relevance_release_id)
        = (baseline_prompt_release_id, baseline_relevance_release_id)
      AND (observed_prompt_release_id, observed_relevance_release_id)
        = (baseline_prompt_release_id, baseline_relevance_release_id)
      AND observed_generation = expected_generation
      AND resulting_generation = observed_generation + 1
    ) OR (
      outcome = 'active_changed'
      AND resulting_generation = observed_generation
      AND (
        (observed_prompt_release_id, observed_relevance_release_id)
          <> (baseline_prompt_release_id, baseline_relevance_release_id)
        OR (observed_prompt_release_id, observed_relevance_release_id)
          <> (expected_prompt_release_id, expected_relevance_release_id)
        OR observed_generation <> expected_generation
      )
    )
  )
);

CREATE TRIGGER release_target_activation_receipts_are_immutable
BEFORE UPDATE OR DELETE ON release_target_activation_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE UNIQUE INDEX release_target_activations_have_one_generation
ON release_target_activation_receipts (resulting_generation)
WHERE outcome = 'activated';

CREATE FUNCTION require_approved_release_target_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM release_target_activation_receipts receipt
    WHERE receipt.outcome = 'activated'
      AND (receipt.baseline_prompt_release_id, receipt.baseline_relevance_release_id)
        = (OLD.prompt_release_id, OLD.relevance_release_id)
      AND (receipt.candidate_prompt_release_id, receipt.candidate_relevance_release_id)
        = (NEW.prompt_release_id, NEW.relevance_release_id)
      AND receipt.observed_generation = OLD.generation
      AND receipt.resulting_generation = NEW.generation
      AND receipt.actor = NEW.activated_by
      AND receipt.requested_at = NEW.activated_at
  ) THEN
    RAISE EXCEPTION 'active release target update requires an approved activation receipt'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER active_release_target_requires_approved_activation
AFTER UPDATE ON active_release_target
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_approved_release_target_activation();

CREATE TRIGGER active_release_target_cannot_be_deleted
BEFORE DELETE ON active_release_target
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

ALTER TABLE pipeline_runs
ADD COLUMN relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
ADD CONSTRAINT orchestration_runs_own_complete_release_target CHECK (
  kind <> 'orchestration'
  OR (prompt_release_id IS NOT NULL AND relevance_release_id IS NOT NULL)
) NOT VALID;

ALTER TABLE pipeline_runs
DROP CONSTRAINT pipeline_runs_configuration_publication_fk,
ADD CONSTRAINT pipeline_runs_configuration_revision_fk
  FOREIGN KEY (configuration_revision_id) REFERENCES search_configuration_revisions(id)
  NOT VALID;

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
  IF run_kind IS NULL OR run_kind <> 'orchestration' THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO snapshot_count
  FROM run_exchange_rate_snapshots WHERE pipeline_run_id = checked_run_id;
  IF prompt_id IS NULL OR relevance_id IS NULL OR snapshot_count <> 1 THEN
    RAISE EXCEPTION 'orchestration run % requires one release target and rate snapshot',
      checked_run_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;
