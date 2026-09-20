ALTER TABLE model_call_attempts
DROP CONSTRAINT model_call_attempts_provider_check,
ADD CONSTRAINT model_call_attempts_provider_check
CHECK (provider IN ('openrouter', 'typesafe'));
