-- Individual accounts replace the installation-wide owner password. A legacy
-- hash remains only until the first admin verifies and claims it.
ALTER TABLE owner_onboarding DROP CONSTRAINT owner_onboarding_check;
ALTER TABLE owner_onboarding ADD CONSTRAINT owner_onboarding_password_hash_check CHECK (
  password_hash IS NULL OR password_hash LIKE 'scrypt$v=1$%'
);
ALTER TABLE owner_onboarding ADD CONSTRAINT owner_onboarding_unclaimed_stage_check CHECK (
  stage NOT IN ('legacy_owner_import', 'owner_account') OR password_hash IS NULL
);

CREATE OR REPLACE FUNCTION protect_owner_onboarding_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'owner onboarding state cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.password_hash IS NOT NULL AND EXISTS (SELECT 1 FROM review_users) THEN
    RAISE EXCEPTION 'shared owner password cannot be restored after admin claim'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.password_hash IS NOT NULL
     AND NEW.password_hash IS DISTINCT FROM OLD.password_hash
     AND NOT (NEW.password_hash IS NULL AND EXISTS (SELECT 1 FROM review_users WHERE role = 'admin')) THEN
    RAISE EXCEPTION 'owner password cannot be replaced or removed before admin claim'
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
