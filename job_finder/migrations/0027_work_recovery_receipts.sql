ALTER TABLE job_work_items ADD COLUMN last_failed_at TIMESTAMPTZ;

UPDATE job_work_items
SET last_failed_at = COALESCE(completed_at, created_at)
WHERE state IN ('failed', 'terminal_error');

UPDATE job_work_items
SET state = 'terminal_error', retry_at = NULL,
    completed_at = CURRENT_TIMESTAMP, last_failed_at = CURRENT_TIMESTAMP
WHERE state = 'failed' AND attempt_count >= 3;

ALTER TABLE job_work_items
ADD CONSTRAINT retryable_job_work_has_attempt_budget
CHECK (state <> 'failed' OR attempt_count < 3);

CREATE TABLE work_recovery_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  job_id UUID NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('retry_now', 'recover_terminal')),
  expected_state TEXT NOT NULL CHECK (expected_state IN ('failed', 'terminal_error')),
  expected_attempt_count INTEGER NOT NULL CHECK (expected_attempt_count >= 0),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  outcome TEXT NOT NULL CHECK (
    outcome IN ('applied', 'stale_state', 'active_lease', 'not_found')
  ),
  prior_state TEXT CHECK (
    prior_state IN ('pending', 'leased', 'failed', 'completed', 'terminal_error')
  ),
  prior_attempt_count INTEGER CHECK (prior_attempt_count >= 0),
  prior_retry_at TIMESTAMPTZ,
  prior_failed_at TIMESTAMPTZ,
  prior_error JSONB,
  resulting_state TEXT CHECK (
    resulting_state IN ('pending', 'leased', 'failed', 'completed', 'terminal_error')
  ),
  resulting_attempt_count INTEGER CHECK (resulting_attempt_count >= 0),
  resulting_retry_at TIMESTAMPTZ,
  CONSTRAINT work_recovery_action_expected_state CHECK (
    (action = 'retry_now' AND expected_state = 'failed')
    OR (action = 'recover_terminal' AND expected_state = 'terminal_error')
  ),
  CONSTRAINT work_recovery_receipt_outcome_shape CHECK (
    (
      outcome = 'not_found'
      AND prior_state IS NULL
      AND prior_attempt_count IS NULL
      AND prior_retry_at IS NULL
      AND prior_failed_at IS NULL
      AND prior_error IS NULL
      AND resulting_state IS NULL
      AND resulting_attempt_count IS NULL
      AND resulting_retry_at IS NULL
    ) OR (
      outcome = 'active_lease'
      AND prior_state = 'leased'
      AND prior_attempt_count IS NOT NULL
      AND prior_retry_at IS NULL
      AND prior_failed_at IS NOT NULL
      AND prior_error IS NULL
      AND resulting_state = prior_state
      AND resulting_attempt_count = prior_attempt_count
      AND resulting_retry_at IS NULL
    ) OR (
      outcome = 'stale_state'
      AND prior_state IS NOT NULL
      AND (
        prior_state <> expected_state
        OR prior_attempt_count <> expected_attempt_count
      )
      AND prior_attempt_count IS NOT NULL
      AND prior_failed_at IS NOT NULL
      AND resulting_state = prior_state
      AND resulting_attempt_count = prior_attempt_count
      AND resulting_retry_at IS NOT DISTINCT FROM prior_retry_at
    ) OR (
      outcome = 'applied'
      AND prior_state = expected_state
      AND prior_attempt_count = expected_attempt_count
      AND prior_failed_at IS NOT NULL
      AND prior_error IS NOT NULL
      AND (
        (
          action = 'retry_now'
          AND prior_retry_at IS NOT NULL
          AND resulting_state = 'failed'
          AND resulting_attempt_count = prior_attempt_count
          AND resulting_retry_at = requested_at
        ) OR (
          action = 'recover_terminal'
          AND prior_retry_at IS NULL
          AND resulting_state = 'pending'
          AND resulting_attempt_count = 0
          AND resulting_retry_at IS NULL
        )
      )
    )
  )
);

CREATE TRIGGER work_recovery_receipts_are_immutable
BEFORE UPDATE OR DELETE ON work_recovery_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
