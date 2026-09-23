CREATE TABLE work_dismissals (
  job_id UUID PRIMARY KEY REFERENCES job_work_items(job_id) ON DELETE CASCADE,
  attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
  actor VARCHAR(200) NOT NULL CHECK (actor <> ''),
  dismissed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE work_dismissal_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY CHECK (idempotency_key <> ''),
  job_id UUID NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('dismiss', 'undo_dismiss')),
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
  resulting_attempt_count INTEGER CHECK (resulting_attempt_count >= 0),
  CONSTRAINT work_dismissal_receipt_outcome_shape CHECK (
    (
      outcome = 'not_found'
      AND prior_state IS NULL
      AND prior_attempt_count IS NULL
      AND resulting_attempt_count IS NULL
    ) OR (
      outcome = 'active_lease'
      AND prior_state = 'leased'
      AND prior_attempt_count IS NOT NULL
      AND resulting_attempt_count = prior_attempt_count
    ) OR (
      outcome = 'stale_state'
      AND prior_state IS NOT NULL
      AND prior_attempt_count IS NOT NULL
      AND (
        prior_state <> 'terminal_error'
        OR prior_attempt_count <> expected_attempt_count
      )
      AND resulting_attempt_count = prior_attempt_count
    ) OR (
      outcome = 'applied'
      AND prior_state = 'terminal_error'
      AND prior_attempt_count = expected_attempt_count
      AND resulting_attempt_count = prior_attempt_count
    )
  )
);

CREATE TRIGGER work_dismissal_receipts_are_immutable
BEFORE UPDATE OR DELETE ON work_dismissal_receipts
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
