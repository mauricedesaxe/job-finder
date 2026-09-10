ALTER TABLE model_call_attempts
ADD COLUMN response_model TEXT,
ADD CONSTRAINT model_call_attempts_accepted_response_model
CHECK ((status = 'accepted') = (response_model IS NOT NULL)),
ADD CONSTRAINT model_call_attempts_accepted_provenance
CHECK (
  status <> 'accepted'
  OR (
    provider_response_id IS NOT NULL
    AND raw_response IS NOT NULL
    AND input_tokens IS NOT NULL
    AND output_tokens IS NOT NULL
    AND cost_usd IS NOT NULL
  )
);
