ALTER TABLE qualification_promotion_decisions
  ALTER COLUMN baseline_target_id DROP NOT NULL;

DROP INDEX qualification_promotion_decisions_target_pair_unique;
CREATE UNIQUE INDEX qualification_promotion_decisions_target_pair_unique
ON qualification_promotion_decisions (baseline_target_id, candidate_target_id) NULLS NOT DISTINCT;

ALTER TABLE qualification_target_activation_receipts
  ALTER COLUMN baseline_target_id DROP NOT NULL;
