ALTER TABLE model_call_attempts
ADD COLUMN request_messages JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE model_call_attempts
ALTER COLUMN request_messages DROP DEFAULT;
