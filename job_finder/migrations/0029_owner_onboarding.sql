CREATE TABLE owner_onboarding (
  singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
  stage TEXT NOT NULL CHECK (
    stage IN ('legacy_owner_import', 'owner_account', 'providers', 'preferences',
              'budget', 'test_search', 'complete')
  ),
  password_hash TEXT,
  initialized_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (stage IN ('legacy_owner_import', 'owner_account') AND password_hash IS NULL)
    OR (stage NOT IN ('legacy_owner_import', 'owner_account')
        AND password_hash IS NOT NULL
        AND password_hash LIKE 'scrypt$v=1$%')
  )
);

CREATE FUNCTION protect_owner_onboarding_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'owner onboarding state cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.password_hash IS NOT NULL
     AND NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
    RAISE EXCEPTION 'owner password cannot be replaced or removed'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.stage = OLD.stage THEN
    RETURN NEW;
  END IF;
  IF NOT (
    (OLD.stage = 'legacy_owner_import' AND NEW.stage = 'complete')
    OR (OLD.stage = 'owner_account' AND NEW.stage = 'providers')
    OR (OLD.stage = 'providers' AND NEW.stage = 'preferences')
    OR (OLD.stage = 'preferences' AND NEW.stage = 'budget')
    OR (OLD.stage = 'budget' AND NEW.stage = 'test_search')
    OR (OLD.stage = 'test_search' AND NEW.stage = 'complete')
  ) THEN
    RAISE EXCEPTION 'invalid owner onboarding transition from % to %', OLD.stage, NEW.stage
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER owner_onboarding_state_is_forward_only
BEFORE UPDATE OR DELETE ON owner_onboarding
FOR EACH ROW EXECUTE FUNCTION protect_owner_onboarding_state();
