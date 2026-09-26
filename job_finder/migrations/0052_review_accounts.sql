CREATE TABLE review_users (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL,
  password_hash TEXT NOT NULL CHECK (password_hash LIKE 'scrypt$v=1$%'),
  role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  grants TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (email = lower(btrim(email)) AND email <> ''),
  CHECK (role = 'member' OR grants = '{}'),
  CHECK (grants <@ ARRAY[
    'review.view', 'review.submit', 'search.view', 'search.draft',
    'search.publish', 'search.activate', 'activity.view', 'activity.recover',
    'activity.dismiss', 'activity.reevaluate', 'analytics.view', 'control.view',
    'control.run', 'control.schedule', 'admin.view', 'admin.members',
    'admin.credentials', 'admin.budget'
  ]::TEXT[])
);

CREATE UNIQUE INDEX review_users_email_ci ON review_users (lower(email));

CREATE FUNCTION protect_last_review_admin()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.role = 'admin' AND OLD.status = 'active'
     AND (TG_OP = 'DELETE' OR NEW.role <> 'admin' OR NEW.status <> 'active') THEN
    PERFORM 1 FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE;
    IF (SELECT count(*) FROM review_users
        WHERE role = 'admin' AND status = 'active' AND id <> OLD.id) = 0 THEN
      RAISE EXCEPTION 'cannot remove the last active admin'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER review_users_keep_active_admin
BEFORE UPDATE OR DELETE ON review_users
FOR EACH ROW EXECUTE FUNCTION protect_last_review_admin();

CREATE TABLE review_sessions (
  token_hash CHAR(64) PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  user_id UUID NOT NULL REFERENCES review_users(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX review_sessions_user_id_idx ON review_sessions (user_id);

CREATE TABLE review_account_tokens (
  token_hash CHAR(64) PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  purpose TEXT NOT NULL CHECK (purpose IN ('invite', 'password_reset')),
  email TEXT,
  grants TEXT[],
  role TEXT CHECK (role IN ('admin', 'member')),
  user_id UUID REFERENCES review_users(id) ON DELETE CASCADE,
  created_by UUID NOT NULL REFERENCES review_users(id),
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (purpose = 'invite' AND email IS NOT NULL AND grants IS NOT NULL
      AND role IS NOT NULL AND user_id IS NULL)
    OR (purpose = 'password_reset' AND email IS NULL AND grants IS NULL
      AND role IS NULL AND user_id IS NOT NULL)
  )
);
CREATE INDEX review_account_tokens_user_id_idx ON review_account_tokens (user_id);

CREATE TABLE review_membership_audit (
  id UUID PRIMARY KEY,
  actor_id UUID NOT NULL REFERENCES review_users(id),
  subject_id UUID REFERENCES review_users(id),
  action TEXT NOT NULL CHECK (action IN ('first_admin', 'invited', 'created', 'grants_changed',
    'role_changed', 'disabled', 'enabled', 'password_reset_issued', 'password_reset')),
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((action = 'invited' AND subject_id IS NULL) OR
         (action <> 'invited' AND subject_id IS NOT NULL))
);

CREATE FUNCTION protect_review_membership_audit()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'membership audit is append-only' USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER review_membership_audit_append_only
BEFORE UPDATE OR DELETE ON review_membership_audit
FOR EACH ROW EXECUTE FUNCTION protect_review_membership_audit();
