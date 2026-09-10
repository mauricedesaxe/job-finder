ALTER TABLE model_call_attempts
DROP CONSTRAINT model_call_attempts_accepted_response_model,
ADD CONSTRAINT model_call_attempts_response_model
CHECK (
  (response_model IS NOT NULL) = (
    status = 'accepted'
    OR (
      status IN ('retryable_error', 'terminal_error')
      AND COALESCE(error ->> 'code' = 'usage_unavailable', FALSE)
    )
  )
);
