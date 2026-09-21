CREATE TABLE relevance_releases (
  id CHAR(64) PRIMARY KEY,
  content_digest CHAR(64) NOT NULL UNIQUE,
  content JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL,
  CONSTRAINT relevance_release_id_matches_digest CHECK (id = content_digest),
  CONSTRAINT relevance_release_content_is_object CHECK (jsonb_typeof(content) = 'object'),
  CONSTRAINT relevance_release_digest_matches_content CHECK (
    content_digest = encode(
      sha256(convert_to(canonical_job_finder_json(content), 'UTF8')),
      'hex'
    )
  )
);

CREATE TRIGGER relevance_releases_are_immutable
BEFORE UPDATE OR DELETE ON relevance_releases
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

ALTER TABLE evaluation_runs
ADD COLUMN relevance_release_id CHAR(64) REFERENCES relevance_releases(id);

ALTER TABLE evaluation_case_results
ADD COLUMN relevance_release_id CHAR(64) REFERENCES relevance_releases(id);

ALTER TABLE evaluation_runs
ADD CONSTRAINT evaluation_runs_require_relevance_release
CHECK (relevance_release_id IS NOT NULL) NOT VALID;

ALTER TABLE evaluation_case_results
ADD CONSTRAINT evaluation_case_results_require_relevance_release
CHECK (relevance_release_id IS NOT NULL) NOT VALID;

ALTER TABLE evaluation_runs
ADD CONSTRAINT evaluation_runs_exact_release_target_key
UNIQUE (id, manifest_id, prompt_release_id, relevance_release_id);

ALTER TABLE evaluation_case_results
ADD CONSTRAINT evaluation_case_results_exact_release_target
FOREIGN KEY (run_id, manifest_id, prompt_release_id, relevance_release_id)
REFERENCES evaluation_runs (id, manifest_id, prompt_release_id, relevance_release_id)
MATCH SIMPLE;

CREATE OR REPLACE FUNCTION require_complete_evaluation_run()
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
           bool_and(r.expected_outcome = c.expected_outcome) AS expectation_matches,
           bool_and(r.relevance_release_id IS NOT DISTINCT FROM run.relevance_release_id)
             AS relevance_matches
    FROM evaluation_case_results r
    JOIN evaluation_runs run ON run.id = r.run_id
    WHERE r.run_id = checked_run_id AND r.case_position = c.position
  ) results ON TRUE
  WHERE c.manifest_id = (
      SELECT manifest_id FROM evaluation_runs WHERE id = checked_run_id
    )
    AND (results.result_count <> c.trial_count
      OR results.distinct_trial_count <> c.trial_count
      OR NOT results.expectation_matches
      OR NOT results.relevance_matches);

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
