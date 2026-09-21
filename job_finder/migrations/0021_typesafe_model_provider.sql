ALTER TABLE model_call_attempts
DROP CONSTRAINT model_call_attempts_provider_check,
ADD CONSTRAINT model_call_attempts_provider_check
CHECK (provider IN ('openrouter', 'typesafe'));

ALTER TABLE model_call_attempts
DROP CONSTRAINT model_call_attempts_accepted_provenance,
ADD CONSTRAINT model_call_attempts_accepted_provenance
CHECK (
  status <> 'accepted'
  OR (
    raw_response IS NOT NULL
    AND input_tokens IS NOT NULL
    AND output_tokens IS NOT NULL
    AND cost_usd IS NOT NULL
    AND (provider <> 'openrouter' OR provider_response_id IS NOT NULL)
  )
);
