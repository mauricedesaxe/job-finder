CREATE TABLE evaluation_case_curations (
  id UUID PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  review_event_id UUID NOT NULL REFERENCES review_events(id),
  action TEXT NOT NULL CHECK (action IN ('include', 'exclude')),
  expected_outcome TEXT CHECK (expected_outcome IN ('qualified', 'rejected')),
  critical BOOLEAN NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (id, review_event_id),
  CHECK ((action = 'include') = (expected_outcome IS NOT NULL)),
  CHECK (action = 'include' OR critical = FALSE)
);

CREATE TRIGGER evaluation_case_curations_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_case_curations
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE evaluation_manifests (
  id CHAR(64) PRIMARY KEY,
  content_digest CHAR(64) NOT NULL UNIQUE,
  expected_case_count INTEGER NOT NULL CHECK (expected_case_count > 0),
  regular_trial_count INTEGER NOT NULL CHECK (regular_trial_count > 0),
  critical_trial_count INTEGER NOT NULL CHECK (critical_trial_count > regular_trial_count),
  max_false_positive_rate NUMERIC(6, 5) NOT NULL CHECK (
    max_false_positive_rate >= 0 AND max_false_positive_rate <= 1
  ),
  max_false_negative_rate NUMERIC(6, 5) NOT NULL CHECK (
    max_false_negative_rate >= 0 AND max_false_negative_rate <= 1
  ),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL,
  CHECK (max_false_positive_rate < max_false_negative_rate)
);

CREATE TABLE evaluation_manifest_cases (
  manifest_id CHAR(64) NOT NULL REFERENCES evaluation_manifests(id),
  position INTEGER NOT NULL CHECK (position >= 0),
  curation_id UUID NOT NULL,
  review_event_id UUID NOT NULL,
  expected_outcome TEXT NOT NULL CHECK (expected_outcome IN ('qualified', 'rejected')),
  critical BOOLEAN NOT NULL,
  trial_count INTEGER NOT NULL CHECK (trial_count > 0),
  input JSONB NOT NULL,
  PRIMARY KEY (manifest_id, position),
  UNIQUE (manifest_id, review_event_id),
  FOREIGN KEY (curation_id, review_event_id)
    REFERENCES evaluation_case_curations(id, review_event_id),
  CHECK (jsonb_typeof(input) = 'object')
);

CREATE FUNCTION require_complete_evaluation_manifest()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  checked_manifest_id CHAR(64);
  expected_count INTEGER;
  actual_count INTEGER;
  invalid_count INTEGER;
BEGIN
  IF TG_TABLE_NAME = 'evaluation_manifests' THEN
    checked_manifest_id := NEW.id;
  ELSE
    checked_manifest_id := COALESCE(NEW.manifest_id, OLD.manifest_id);
  END IF;

  SELECT expected_case_count INTO expected_count
  FROM evaluation_manifests
  WHERE id = checked_manifest_id;

  IF expected_count IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT count(*) INTO actual_count
  FROM evaluation_manifest_cases
  WHERE manifest_id = checked_manifest_id;

  IF actual_count <> expected_count THEN
    RAISE EXCEPTION 'evaluation manifest % requires % cases, found %',
      checked_manifest_id, expected_count, actual_count
      USING ERRCODE = 'check_violation';
  END IF;

  SELECT count(*) INTO invalid_count
  FROM evaluation_manifest_cases c
  JOIN evaluation_manifests m ON m.id = c.manifest_id
  JOIN evaluation_case_curations u ON u.id = c.curation_id
  WHERE c.manifest_id = checked_manifest_id
    AND (u.action <> 'include'
      OR u.review_event_id <> c.review_event_id
      OR u.expected_outcome <> c.expected_outcome
      OR u.critical <> c.critical
      OR c.trial_count <> CASE WHEN c.critical
        THEN m.critical_trial_count ELSE m.regular_trial_count END);

  IF invalid_count <> 0 THEN
    RAISE EXCEPTION 'evaluation manifest % contains invalid cases', checked_manifest_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER evaluation_manifests_are_complete
AFTER INSERT OR UPDATE ON evaluation_manifests
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_evaluation_manifest();

CREATE CONSTRAINT TRIGGER evaluation_manifest_cases_keep_manifest_complete
AFTER INSERT OR UPDATE OR DELETE ON evaluation_manifest_cases
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_evaluation_manifest();

CREATE TRIGGER evaluation_manifests_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_manifests
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TRIGGER evaluation_manifest_cases_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_manifest_cases
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE evaluation_runs (
  id CHAR(64) PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  manifest_id CHAR(64) NOT NULL REFERENCES evaluation_manifests(id),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  expected_result_count INTEGER NOT NULL CHECK (expected_result_count > 0),
  result_count INTEGER NOT NULL CHECK (result_count = expected_result_count),
  false_positive_count INTEGER NOT NULL CHECK (false_positive_count >= 0),
  false_negative_count INTEGER NOT NULL CHECK (false_negative_count >= 0),
  operational_failure_count INTEGER NOT NULL CHECK (operational_failure_count >= 0),
  critical_false_positive_count INTEGER NOT NULL CHECK (critical_false_positive_count >= 0),
  false_positive_rate NUMERIC(8, 7) NOT NULL CHECK (
    false_positive_rate >= 0 AND false_positive_rate <= 1
  ),
  false_negative_rate NUMERIC(8, 7) NOT NULL CHECK (
    false_negative_rate >= 0 AND false_negative_rate <= 1
  ),
  implementation_ref TEXT NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (id, manifest_id, prompt_release_id),
  CHECK (
    false_positive_count + false_negative_count + operational_failure_count <= result_count
  )
);

CREATE TABLE evaluation_case_results (
  id CHAR(64) PRIMARY KEY,
  run_id CHAR(64) NOT NULL,
  manifest_id CHAR(64) NOT NULL,
  prompt_release_id CHAR(64) NOT NULL,
  case_position INTEGER NOT NULL,
  trial_index INTEGER NOT NULL CHECK (trial_index >= 0),
  expected_outcome TEXT NOT NULL CHECK (expected_outcome IN ('qualified', 'rejected')),
  actual_outcome TEXT CHECK (actual_outcome IN ('qualified', 'rejected')),
  failure_kind TEXT CHECK (
    failure_kind IN ('false_positive', 'false_negative', 'operational')
  ),
  reason TEXT NOT NULL,
  UNIQUE (run_id, case_position, trial_index),
  FOREIGN KEY (run_id, manifest_id, prompt_release_id)
    REFERENCES evaluation_runs(id, manifest_id, prompt_release_id),
  FOREIGN KEY (manifest_id, case_position)
    REFERENCES evaluation_manifest_cases(manifest_id, position),
  CHECK (
    (failure_kind IS NULL AND actual_outcome = expected_outcome)
    OR (failure_kind = 'false_positive' AND expected_outcome = 'rejected'
        AND actual_outcome = 'qualified')
    OR (failure_kind = 'false_negative' AND expected_outcome = 'qualified'
        AND actual_outcome = 'rejected')
    OR (failure_kind = 'operational' AND actual_outcome IS NULL)
  )
);

CREATE FUNCTION require_complete_evaluation_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  checked_run_id CHAR(64);
  expected_count INTEGER;
  actual_count INTEGER;
  invalid_count INTEGER;
  stored_false_positives INTEGER;
  stored_false_negatives INTEGER;
  stored_operational_failures INTEGER;
  stored_critical_false_positives INTEGER;
  stored_false_positive_rate NUMERIC(8, 7);
  stored_false_negative_rate NUMERIC(8, 7);
  calculated_false_positives INTEGER;
  calculated_false_negatives INTEGER;
  calculated_operational_failures INTEGER;
  calculated_critical_false_positives INTEGER;
  calculated_false_positive_rate NUMERIC(8, 7);
  calculated_false_negative_rate NUMERIC(8, 7);
BEGIN
  IF TG_TABLE_NAME = 'evaluation_runs' THEN
    checked_run_id := NEW.id;
  ELSE
    checked_run_id := COALESCE(NEW.run_id, OLD.run_id);
  END IF;

  SELECT expected_result_count INTO expected_count
  FROM evaluation_runs
  WHERE id = checked_run_id;

  IF expected_count IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT count(*) INTO actual_count
  FROM evaluation_case_results
  WHERE run_id = checked_run_id;

  IF actual_count <> expected_count THEN
    RAISE EXCEPTION 'evaluation run % requires % results, found %',
      checked_run_id, expected_count, actual_count
      USING ERRCODE = 'check_violation';
  END IF;

  SELECT count(*) INTO invalid_count
  FROM evaluation_manifest_cases c
  LEFT JOIN LATERAL (
    SELECT count(*) AS result_count,
           count(DISTINCT r.trial_index) AS distinct_trial_count,
           bool_and(r.expected_outcome = c.expected_outcome) AS expectation_matches
    FROM evaluation_case_results r
    WHERE r.run_id = checked_run_id AND r.case_position = c.position
  ) results ON TRUE
  WHERE c.manifest_id = (
      SELECT manifest_id FROM evaluation_runs WHERE id = checked_run_id
    )
    AND (results.result_count <> c.trial_count
      OR results.distinct_trial_count <> c.trial_count
      OR NOT results.expectation_matches);

  IF invalid_count <> 0 THEN
    RAISE EXCEPTION 'evaluation run % does not cover every configured trial', checked_run_id
      USING ERRCODE = 'check_violation';
  END IF;

  SELECT false_positive_count, false_negative_count, operational_failure_count,
         critical_false_positive_count, false_positive_rate, false_negative_rate
  INTO stored_false_positives, stored_false_negatives, stored_operational_failures,
       stored_critical_false_positives, stored_false_positive_rate,
       stored_false_negative_rate
  FROM evaluation_runs
  WHERE id = checked_run_id;

  SELECT count(*) FILTER (WHERE r.failure_kind = 'false_positive'),
         count(*) FILTER (WHERE r.failure_kind = 'false_negative'),
         count(*) FILTER (WHERE r.failure_kind = 'operational'),
         count(*) FILTER (WHERE r.failure_kind = 'false_positive' AND c.critical),
         CASE WHEN count(*) FILTER (WHERE r.expected_outcome = 'rejected') = 0 THEN 0
           ELSE round(
             (count(*) FILTER (WHERE r.failure_kind = 'false_positive'))::numeric
             / count(*) FILTER (WHERE r.expected_outcome = 'rejected'), 7
           ) END,
         CASE WHEN count(*) FILTER (WHERE r.expected_outcome = 'qualified') = 0 THEN 0
           ELSE round(
             (count(*) FILTER (WHERE r.failure_kind = 'false_negative'))::numeric
             / count(*) FILTER (WHERE r.expected_outcome = 'qualified'), 7
           ) END
  INTO calculated_false_positives, calculated_false_negatives,
       calculated_operational_failures, calculated_critical_false_positives,
       calculated_false_positive_rate, calculated_false_negative_rate
  FROM evaluation_case_results r
  JOIN evaluation_manifest_cases c
    ON c.manifest_id = r.manifest_id AND c.position = r.case_position
  WHERE r.run_id = checked_run_id;

  IF (stored_false_positives, stored_false_negatives, stored_operational_failures,
      stored_critical_false_positives, stored_false_positive_rate,
      stored_false_negative_rate)
     IS DISTINCT FROM
     (calculated_false_positives, calculated_false_negatives,
      calculated_operational_failures, calculated_critical_false_positives,
      calculated_false_positive_rate, calculated_false_negative_rate) THEN
    RAISE EXCEPTION 'evaluation run % metrics do not match its results', checked_run_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER evaluation_runs_are_complete
AFTER INSERT OR UPDATE ON evaluation_runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_evaluation_run();

CREATE CONSTRAINT TRIGGER evaluation_case_results_keep_run_complete
AFTER INSERT OR UPDATE OR DELETE ON evaluation_case_results
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_complete_evaluation_run();

CREATE TRIGGER evaluation_runs_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_runs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TRIGGER evaluation_case_results_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_case_results
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE prompt_promotion_decisions (
  id CHAR(64) PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  manifest_id CHAR(64) NOT NULL REFERENCES evaluation_manifests(id),
  baseline_run_id CHAR(64) NOT NULL,
  baseline_prompt_release_id CHAR(64) NOT NULL,
  candidate_run_id CHAR(64) NOT NULL,
  candidate_prompt_release_id CHAR(64) NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
  reason TEXT NOT NULL,
  baseline_metrics JSONB NOT NULL,
  candidate_metrics JSONB NOT NULL,
  actor TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (baseline_run_id, manifest_id, baseline_prompt_release_id)
    REFERENCES evaluation_runs(id, manifest_id, prompt_release_id),
  FOREIGN KEY (candidate_run_id, manifest_id, candidate_prompt_release_id)
    REFERENCES evaluation_runs(id, manifest_id, prompt_release_id),
  CHECK (baseline_prompt_release_id <> candidate_prompt_release_id),
  CHECK (jsonb_typeof(baseline_metrics) = 'object'),
  CHECK (jsonb_typeof(candidate_metrics) = 'object')
);

CREATE TRIGGER prompt_promotion_decisions_are_immutable
BEFORE UPDATE OR DELETE ON prompt_promotion_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
