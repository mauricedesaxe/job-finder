CREATE TABLE provider_credentials (
  provider TEXT PRIMARY KEY CHECK (provider IN ('jina', 'openrouter', 'typesafe')),
  generation BIGINT NOT NULL CHECK (generation >= 1),
  nonce BYTEA NOT NULL CHECK (octet_length(nonce) = 12),
  ciphertext BYTEA NOT NULL CHECK (octet_length(ciphertext) >= 16),
  capabilities TEXT[] NOT NULL,
  validated_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  updated_by TEXT NOT NULL CHECK (updated_by <> ''),
  CHECK (
    (provider = 'jina' AND capabilities = ARRAY['search', 'scrape']::TEXT[])
    OR (provider = 'openrouter'
        AND capabilities = ARRAY['structured_generation', 'usage_cost']::TEXT[])
    OR (provider = 'typesafe'
        AND capabilities = ARRAY['relevance_evaluation', 'usage_cost']::TEXT[])
  )
);

CREATE FUNCTION protect_provider_credentials()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'provider credentials cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.provider <> OLD.provider OR NEW.generation <> OLD.generation + 1 THEN
    RAISE EXCEPTION 'provider credential replacement must increment generation once'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER provider_credentials_are_versioned
BEFORE UPDATE OR DELETE ON provider_credentials
FOR EACH ROW EXECUTE FUNCTION protect_provider_credentials();
