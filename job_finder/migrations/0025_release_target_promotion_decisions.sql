ALTER TABLE prompt_promotion_decisions
ADD COLUMN baseline_relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
ADD COLUMN candidate_relevance_release_id CHAR(64) REFERENCES relevance_releases(id),
ADD COLUMN comparison_id CHAR(64);

UPDATE prompt_promotion_decisions decisions
SET baseline_relevance_release_id = baseline.relevance_release_id,
    candidate_relevance_release_id = candidate.relevance_release_id,
    comparison_id = encode(sha256(convert_to(
      'evaluation_run_comparison_v1:' || decisions.manifest_id || ':'
      || decisions.baseline_run_id || ':' || decisions.candidate_run_id,
      'UTF8'
    )), 'hex')
FROM evaluation_runs baseline, evaluation_runs candidate
WHERE baseline.id = decisions.baseline_run_id
  AND candidate.id = decisions.candidate_run_id;

ALTER TABLE prompt_promotion_decisions
DROP CONSTRAINT prompt_promotion_decisions_check,
ADD CONSTRAINT prompt_promotion_decisions_distinct_targets CHECK (
  baseline_prompt_release_id <> candidate_prompt_release_id
  OR baseline_relevance_release_id <> candidate_relevance_release_id
),
ADD CONSTRAINT prompt_promotion_decisions_require_exact_targets CHECK (
  baseline_relevance_release_id IS NOT NULL
  AND candidate_relevance_release_id IS NOT NULL
  AND comparison_id IS NOT NULL
) NOT VALID,
ADD CONSTRAINT prompt_promotion_decisions_comparison_id_matches CHECK (
  comparison_id IS NULL OR comparison_id = encode(sha256(convert_to(
    'evaluation_run_comparison_v1:' || manifest_id || ':'
    || baseline_run_id || ':' || candidate_run_id,
    'UTF8'
  )), 'hex')
),
ADD CONSTRAINT prompt_promotion_decisions_exact_baseline FOREIGN KEY (
  baseline_run_id, manifest_id, baseline_prompt_release_id,
  baseline_relevance_release_id
) REFERENCES evaluation_runs (
  id, manifest_id, prompt_release_id, relevance_release_id
) MATCH SIMPLE,
ADD CONSTRAINT prompt_promotion_decisions_exact_candidate FOREIGN KEY (
  candidate_run_id, manifest_id, candidate_prompt_release_id,
  candidate_relevance_release_id
) REFERENCES evaluation_runs (
  id, manifest_id, prompt_release_id, relevance_release_id
) MATCH SIMPLE,
ADD CONSTRAINT prompt_promotion_decisions_one_per_comparison
  UNIQUE (baseline_run_id, candidate_run_id);
