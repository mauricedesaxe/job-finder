CREATE TABLE qualification_promotion_decisions (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
  baseline_target_id CHAR(64) NOT NULL REFERENCES qualification_targets(id),
  candidate_target_id CHAR(64) NOT NULL REFERENCES qualification_targets(id),
  input_preparation_evidence_id CHAR(64) REFERENCES qualification_phase_evidence(id),
  relevance_evidence_id CHAR(64) REFERENCES qualification_phase_evidence(id),
  enrichment_evidence_id CHAR(64) REFERENCES qualification_phase_evidence(id),
  deduplication_evidence_id CHAR(64) REFERENCES qualification_phase_evidence(id),
  composition_evidence_id CHAR(64) REFERENCES qualification_phase_evidence(id),
  relevance_comparison_id CHAR(64) REFERENCES qualification_relevance_comparisons(id),
  decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
  reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
  actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
  created_at TIMESTAMPTZ NOT NULL,
  CHECK (baseline_target_id <> candidate_target_id),
  CHECK (decision <> 'approved' OR composition_evidence_id IS NOT NULL)
);
CREATE TRIGGER qualification_promotion_decisions_are_immutable
BEFORE UPDATE OR DELETE ON qualification_promotion_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE active_qualification_target (
  singleton_id INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
  target_id CHAR(64) REFERENCES qualification_targets(id),
  generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
  activated_at TIMESTAMPTZ,
  activated_by TEXT,
  CHECK (
    (target_id IS NULL AND generation = 0 AND activated_at IS NULL AND activated_by IS NULL)
    OR (target_id IS NOT NULL AND generation > 0 AND activated_at IS NOT NULL
        AND activated_by IS NOT NULL AND btrim(activated_by) <> '')
  )
);
INSERT INTO active_qualification_target (singleton_id) VALUES (1);

CREATE TABLE qualification_target_activation_receipts (
  idempotency_key TEXT PRIMARY KEY CHECK (idempotency_key <> ''),
  outcome TEXT NOT NULL CHECK (outcome IN ('activated', 'active_changed')),
  promotion_decision_id CHAR(64) NOT NULL REFERENCES qualification_promotion_decisions(id),
  baseline_target_id CHAR(64) NOT NULL REFERENCES qualification_targets(id),
  candidate_target_id CHAR(64) NOT NULL REFERENCES qualification_targets(id),
  expected_target_id CHAR(64) REFERENCES qualification_targets(id),
  expected_generation BIGINT NOT NULL CHECK (expected_generation >= 0),
  observed_target_id CHAR(64) REFERENCES qualification_targets(id),
  observed_generation BIGINT NOT NULL CHECK (observed_generation >= 0),
  observed_activated_at TIMESTAMPTZ,
  observed_activated_by TEXT,
  resulting_generation BIGINT NOT NULL CHECK (resulting_generation >= 0),
  actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  CHECK (
    (observed_target_id IS NULL AND observed_generation = 0
      AND observed_activated_at IS NULL AND observed_activated_by IS NULL)
    OR (observed_target_id IS NOT NULL AND observed_generation > 0
      AND observed_activated_at IS NOT NULL AND observed_activated_by IS NOT NULL)
  ),
  CHECK (
    (outcome = 'activated' AND resulting_generation = observed_generation + 1)
    OR (outcome = 'active_changed' AND resulting_generation = observed_generation)
  )
);
CREATE TRIGGER qualification_target_activation_receipts_are_immutable
BEFORE UPDATE OR DELETE ON qualification_target_activation_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
