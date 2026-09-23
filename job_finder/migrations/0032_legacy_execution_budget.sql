INSERT INTO execution_budget_policy (
  singleton_id, version, monthly_limit_usd, run_allowance_usd,
  max_jobs_per_run, max_search_queries_per_run,
  max_provider_attempts_per_run, updated_at, updated_by
)
SELECT
  1,
  1,
  500,
  50,
  100,
  10000,
  1000000,
  CURRENT_TIMESTAMP,
  'migration:0032_legacy_execution_budget.sql'
WHERE EXISTS (
  SELECT 1
  FROM owner_onboarding
  WHERE singleton_id = 1
    AND stage IN ('legacy_owner_import', 'complete')
)
AND NOT EXISTS (
  SELECT 1 FROM execution_budget_policy WHERE singleton_id = 1
);
