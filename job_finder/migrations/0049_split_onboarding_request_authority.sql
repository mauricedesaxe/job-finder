ALTER TABLE execution_budget_reservations
ADD CONSTRAINT execution_budget_reservation_split_identity_unique
UNIQUE (
  idempotency_key, acquisition_policy_revision_id, qualification_target_id,
  acquisition_generation, qualification_generation
);

ALTER TABLE onboarding_test_search_requests
ALTER COLUMN configuration_revision_id DROP NOT NULL,
ALTER COLUMN prompt_release_id DROP NOT NULL,
ALTER COLUMN relevance_release_id DROP NOT NULL,
ALTER COLUMN release_generation DROP NOT NULL,
ADD COLUMN execution_authority_kind TEXT NOT NULL DEFAULT 'legacy'
  CHECK (execution_authority_kind IN ('legacy', 'split')),
ADD COLUMN acquisition_policy_revision_id CHAR(64)
  REFERENCES acquisition_policy_revisions(id),
ADD COLUMN qualification_target_id CHAR(64)
  REFERENCES qualification_targets(id),
ADD COLUMN acquisition_generation BIGINT CHECK (acquisition_generation >= 0),
ADD COLUMN qualification_generation BIGINT CHECK (qualification_generation >= 0),
ADD CONSTRAINT onboarding_test_search_request_authority_is_complete CHECK (
  (
    execution_authority_kind = 'legacy'
    AND configuration_revision_id IS NOT NULL
    AND prompt_release_id IS NOT NULL
    AND relevance_release_id IS NOT NULL
    AND release_generation IS NOT NULL
    AND acquisition_policy_revision_id IS NULL
    AND qualification_target_id IS NULL
    AND acquisition_generation IS NULL
    AND qualification_generation IS NULL
  ) OR (
    execution_authority_kind = 'split'
    AND configuration_revision_id IS NULL
    AND prompt_release_id IS NULL
    AND relevance_release_id IS NULL
    AND release_generation IS NULL
    AND acquisition_policy_revision_id IS NOT NULL
    AND qualification_target_id IS NOT NULL
    AND acquisition_generation IS NOT NULL
    AND qualification_generation IS NOT NULL
  )
),
ADD CONSTRAINT onboarding_test_search_request_matches_split_reservation
FOREIGN KEY (
  budget_reservation_key, acquisition_policy_revision_id, qualification_target_id,
  acquisition_generation, qualification_generation
)
REFERENCES execution_budget_reservations (
  idempotency_key, acquisition_policy_revision_id, qualification_target_id,
  acquisition_generation, qualification_generation
);

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
     OR NEW.execution_authority_kind <> OLD.execution_authority_kind
     OR NEW.configuration_revision_id IS DISTINCT FROM OLD.configuration_revision_id
     OR NEW.prompt_release_id IS DISTINCT FROM OLD.prompt_release_id
     OR NEW.relevance_release_id IS DISTINCT FROM OLD.relevance_release_id
     OR NEW.release_generation IS DISTINCT FROM OLD.release_generation
     OR NEW.acquisition_policy_revision_id IS DISTINCT FROM OLD.acquisition_policy_revision_id
     OR NEW.qualification_target_id IS DISTINCT FROM OLD.qualification_target_id
     OR NEW.acquisition_generation IS DISTINCT FROM OLD.acquisition_generation
     OR NEW.qualification_generation IS DISTINCT FROM OLD.qualification_generation
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
  IF NEW.attempt_count < OLD.attempt_count THEN
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
