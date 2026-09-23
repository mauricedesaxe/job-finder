CREATE TABLE onboarding_test_search_requests (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  run_id UUID NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'completed', 'failed')),
  configuration_revision_id CHAR(64) NOT NULL
    REFERENCES search_configuration_revisions(id),
  prompt_release_id CHAR(64) NOT NULL
    REFERENCES prompt_releases(id),
  relevance_release_id CHAR(64) NOT NULL
    REFERENCES relevance_releases(id),
  release_generation BIGINT NOT NULL CHECK (release_generation >= 0),
  budget_policy_version BIGINT NOT NULL CHECK (budget_policy_version >= 1),
  budget_reservation_key TEXT NOT NULL UNIQUE
    REFERENCES execution_budget_reservations(idempotency_key),
  max_queries INTEGER NOT NULL CHECK (max_queries >= 1),
  max_urls INTEGER NOT NULL CHECK (max_urls >= 1),
  max_jobs INTEGER NOT NULL CHECK (max_jobs >= 1),
  max_work_attempts INTEGER NOT NULL CHECK (max_work_attempts >= 1),
  max_provider_attempts INTEGER NOT NULL CHECK (max_provider_attempts >= 1),
  run_allowance_usd NUMERIC(18, 8) NOT NULL CHECK (run_allowance_usd > 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  owner_token UUID,
  lease_expires_at TIMESTAMPTZ,
  error_code TEXT CHECK (error_code <> ''),
  error_reason TEXT CHECK (error_reason <> ''),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  CHECK (
    (
      state = 'pending'
      AND owner_token IS NULL
      AND lease_expires_at IS NULL
      AND completed_at IS NULL
      AND error_code IS NULL
      AND error_reason IS NULL
    ) OR (
      state = 'leased'
      AND owner_token IS NOT NULL
      AND lease_expires_at IS NOT NULL
      AND completed_at IS NULL
      AND error_code IS NULL
      AND error_reason IS NULL
    ) OR (
      state = 'completed'
      AND completed_at IS NOT NULL
      AND error_code IS NULL
      AND error_reason IS NULL
    ) OR (
      state = 'failed'
      AND completed_at IS NOT NULL
      AND error_code IS NOT NULL
      AND error_reason IS NOT NULL
    )
  )
);

CREATE INDEX onboarding_test_search_claimable
  ON onboarding_test_search_requests (created_at, idempotency_key)
  WHERE state IN ('pending', 'leased');

CREATE FUNCTION protect_onboarding_test_search_request()
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
     OR NEW.configuration_revision_id <> OLD.configuration_revision_id
     OR NEW.prompt_release_id <> OLD.prompt_release_id
     OR NEW.relevance_release_id <> OLD.relevance_release_id
     OR NEW.release_generation <> OLD.release_generation
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

CREATE TRIGGER onboarding_test_search_requests_are_guarded
BEFORE UPDATE OR DELETE ON onboarding_test_search_requests
FOR EACH ROW EXECUTE FUNCTION protect_onboarding_test_search_request();
