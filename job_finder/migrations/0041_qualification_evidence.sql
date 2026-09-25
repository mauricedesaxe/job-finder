CREATE TABLE relevance_experiment_inputs (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  manifest_id CHAR(64) NOT NULL REFERENCES evaluation_manifests(id),
  content JSONB NOT NULL CHECK (
    COALESCE(
      jsonb_typeof(content) = 'object'
      AND content ?& ARRAY[
        'schema_version', 'manifest_id', 'exchange_rates',
        'provider_settings', 'input_path'
      ]
      AND content - ARRAY[
        'schema_version', 'manifest_id', 'exchange_rates',
        'provider_settings', 'input_path'
      ] = '{}'::JSONB
      AND jsonb_typeof(content -> 'schema_version') = 'number'
      AND content ->> 'schema_version' = '1'
      AND content ->> 'manifest_id' = manifest_id
      AND jsonb_typeof(content -> 'exchange_rates') = 'object'
      AND jsonb_typeof(content -> 'provider_settings') = 'object'
      AND content ->> 'input_path' IN ('direct', 'ats'),
      FALSE
    )
  ),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT relevance_experiment_input_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  )
);
CREATE TRIGGER relevance_experiment_inputs_are_immutable
BEFORE UPDATE OR DELETE ON relevance_experiment_inputs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE qualification_fixture_sets (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  phase TEXT NOT NULL CHECK (
    phase IN ('input_preparation', 'enrichment', 'deduplication', 'composition')
  ),
  content JSONB NOT NULL CHECK (
    COALESCE(
      jsonb_typeof(content) = 'object'
      AND content ?& ARRAY['schema_version', 'phase', 'cases']
      AND content - ARRAY['schema_version', 'phase', 'cases'] = '{}'::JSONB
      AND jsonb_typeof(content -> 'schema_version') = 'number'
      AND content ->> 'schema_version' = '1'
      AND content ->> 'phase' = phase
      AND jsonb_typeof(content -> 'cases') = 'array'
      AND jsonb_array_length(content -> 'cases') > 0,
      FALSE
    )
  ),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT qualification_fixture_set_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  )
);
CREATE TRIGGER qualification_fixture_sets_are_immutable
BEFORE UPDATE OR DELETE ON qualification_fixture_sets
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE TABLE qualification_phase_evidence (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  target_id CHAR(64) NOT NULL REFERENCES qualification_targets(id),
  phase TEXT NOT NULL CHECK (
    phase IN ('input_preparation', 'relevance', 'enrichment', 'deduplication', 'composition')
  ),
  component_release_id CHAR(64) REFERENCES qualification_component_releases(id),
  experiment_input_id CHAR(64) REFERENCES relevance_experiment_inputs(id),
  fixture_set_id CHAR(64) REFERENCES qualification_fixture_sets(id),
  executor_artifact_id CHAR(64) NOT NULL REFERENCES implementation_artifacts(id),
  origin TEXT NOT NULL CHECK (origin IN ('canonical', 'synthetic', 'imported')),
  outcome TEXT NOT NULL CHECK (outcome IN ('passed', 'failed')),
  content JSONB NOT NULL CHECK (
    COALESCE(
      jsonb_typeof(content) = 'object'
      AND content ?& ARRAY[
        'schema_version', 'target_id', 'phase', 'component_release_id',
        'experiment_input_id', 'fixture_set_id', 'executor_artifact_id',
        'origin', 'outcome', 'result', 'attempts', 'completed_at'
      ]
      AND content - ARRAY[
        'schema_version', 'target_id', 'phase', 'component_release_id',
        'experiment_input_id', 'fixture_set_id', 'executor_artifact_id',
        'origin', 'outcome', 'result', 'attempts', 'completed_at'
      ] = '{}'::JSONB
      AND jsonb_typeof(content -> 'schema_version') = 'number'
      AND content ->> 'schema_version' = '1'
      AND content ->> 'target_id' = target_id
      AND content ->> 'phase' = phase
      AND content -> 'component_release_id' =
        COALESCE(to_jsonb(component_release_id), 'null'::JSONB)
      AND content -> 'experiment_input_id' =
        COALESCE(to_jsonb(experiment_input_id), 'null'::JSONB)
      AND content -> 'fixture_set_id' =
        COALESCE(to_jsonb(fixture_set_id), 'null'::JSONB)
      AND content ->> 'executor_artifact_id' = executor_artifact_id
      AND content ->> 'origin' = origin
      AND content ->> 'outcome' = outcome
      AND jsonb_typeof(content -> 'result') = 'object'
      AND jsonb_typeof(content -> 'attempts') = 'array',
      FALSE
    )
  ),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CONSTRAINT qualification_phase_evidence_matches_content CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(content), 'UTF8')), 'hex')
  ),
  CONSTRAINT qualification_phase_evidence_input_shape CHECK (
    (phase = 'relevance' AND component_release_id IS NOT NULL
      AND experiment_input_id IS NOT NULL AND fixture_set_id IS NULL)
    OR (phase = 'composition' AND component_release_id IS NULL
      AND experiment_input_id IS NULL AND fixture_set_id IS NOT NULL)
    OR (phase IN ('input_preparation', 'enrichment', 'deduplication')
      AND component_release_id IS NOT NULL
      AND experiment_input_id IS NULL AND fixture_set_id IS NOT NULL)
  )
);
CREATE TRIGGER qualification_phase_evidence_is_immutable
BEFORE UPDATE OR DELETE ON qualification_phase_evidence
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_qualification_evidence_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  target RECORD;
  fixture_phase TEXT;
BEGIN
  SELECT artifact_id, input_preparation_release_id, relevance_release_id,
         enrichment_release_id, deduplication_release_id
  INTO target FROM qualification_targets WHERE id = NEW.target_id;
  IF target.artifact_id IS DISTINCT FROM NEW.executor_artifact_id THEN
    RAISE EXCEPTION 'qualification evidence executor differs from target artifact'
      USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.phase = 'input_preparation'
      AND NEW.component_release_id IS DISTINCT FROM target.input_preparation_release_id)
    OR (NEW.phase = 'relevance'
      AND NEW.component_release_id IS DISTINCT FROM target.relevance_release_id)
    OR (NEW.phase = 'enrichment'
      AND NEW.component_release_id IS DISTINCT FROM target.enrichment_release_id)
    OR (NEW.phase = 'deduplication'
      AND NEW.component_release_id IS DISTINCT FROM target.deduplication_release_id)
  THEN
    RAISE EXCEPTION 'qualification evidence component differs from target'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.fixture_set_id IS NOT NULL THEN
    SELECT phase INTO fixture_phase FROM qualification_fixture_sets
    WHERE id = NEW.fixture_set_id;
    IF fixture_phase IS DISTINCT FROM NEW.phase THEN
      RAISE EXCEPTION 'qualification evidence fixture phase differs from result phase'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER qualification_phase_evidence_requires_target
BEFORE INSERT ON qualification_phase_evidence
FOR EACH ROW EXECUTE FUNCTION require_qualification_evidence_target();

CREATE TABLE qualification_relevance_comparisons (
  id CHAR(64) PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
  experiment_input_id CHAR(64) NOT NULL REFERENCES relevance_experiment_inputs(id),
  baseline_evidence_id CHAR(64) NOT NULL REFERENCES qualification_phase_evidence(id),
  candidate_evidence_id CHAR(64) NOT NULL REFERENCES qualification_phase_evidence(id),
  created_at TIMESTAMPTZ NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by <> ''),
  CHECK (baseline_evidence_id <> candidate_evidence_id),
  CONSTRAINT qualification_relevance_comparison_identity CHECK (
    id = encode(sha256(convert_to(canonical_job_finder_json(jsonb_build_object(
      'experiment_input_id', experiment_input_id,
      'baseline_evidence_id', baseline_evidence_id,
      'candidate_evidence_id', candidate_evidence_id
    )), 'UTF8')), 'hex')
  )
);
CREATE TRIGGER qualification_relevance_comparisons_are_immutable
BEFORE UPDATE OR DELETE ON qualification_relevance_comparisons
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

CREATE FUNCTION require_comparable_qualification_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  baseline RECORD;
  candidate RECORD;
BEGIN
  SELECT phase, experiment_input_id INTO baseline
  FROM qualification_phase_evidence WHERE id = NEW.baseline_evidence_id;
  SELECT phase, experiment_input_id INTO candidate
  FROM qualification_phase_evidence WHERE id = NEW.candidate_evidence_id;
  IF baseline.phase IS DISTINCT FROM 'relevance'
    OR candidate.phase IS DISTINCT FROM 'relevance'
    OR baseline.experiment_input_id IS DISTINCT FROM NEW.experiment_input_id
    OR candidate.experiment_input_id IS DISTINCT FROM NEW.experiment_input_id
  THEN
    RAISE EXCEPTION 'relevance comparison requires the same frozen experiment input'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER qualification_relevance_comparisons_share_input
BEFORE INSERT ON qualification_relevance_comparisons
FOR EACH ROW EXECUTE FUNCTION require_comparable_qualification_evidence();
