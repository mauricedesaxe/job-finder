export const NOTION_BACKFILL_STATS_SQL = `
  SELECT
    (SELECT COUNT(*) FROM processed_jobs WHERE source_key LIKE 'notion:%') AS sourceRows,
    (SELECT COUNT(DISTINCT raw_url) FROM processed_jobs
      WHERE source_key LIKE 'notion:%' AND raw_url IS NOT NULL) AS urls,
    (SELECT COUNT(*) FROM (
      SELECT normalized_company, normalized_title
      FROM processed_jobs
      WHERE source_key LIKE 'notion:%'
      GROUP BY normalized_company, normalized_title
    )) AS companyTitlePairs,
    (SELECT COUNT(*) FROM processed_jobs
      WHERE source_key LIKE 'notion:%' AND raw_url IS NULL) AS urlLessRows,
    (SELECT COUNT(*) FROM company_exclusions WHERE source_key LIKE 'notion:%') AS exclusions
`;

export const MARK_MIGRATION_SQL = `
  INSERT INTO job_ledger_migrations (name, completed_at)
  VALUES (?, ?)
  ON CONFLICT(name) DO UPDATE SET completed_at = excluded.completed_at
`;

export const HAS_MIGRATION_SQL = `
  SELECT name FROM job_ledger_migrations WHERE name = ?
`;

export const FIND_BY_RAW_URL_SQL = `
  SELECT source_key, raw_url, company, title, outcome,
    first_processed_at, last_processed_at, trace_id
  FROM processed_jobs
  WHERE raw_url = ?
`;

export const TITLES_FOR_COMPANY_SQL = `
  SELECT DISTINCT title
  FROM processed_jobs
  WHERE normalized_company = ? AND outcome <> 'duplicated'
  ORDER BY normalized_title, title
`;

export const FIND_COMPANY_EXCLUSION_SQL = `
  SELECT company, excluded_at
  FROM company_exclusions
  WHERE normalized_company = ?
`;

export const RECORD_PROCESSED_JOB_SQL = `
  INSERT INTO processed_jobs (
    source_key,
    raw_url,
    company,
    normalized_company,
    title,
    normalized_title,
    outcome,
    first_processed_at,
    last_processed_at,
    trace_id
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(source_key) DO UPDATE SET
    raw_url = excluded.raw_url,
    company = excluded.company,
    normalized_company = excluded.normalized_company,
    title = excluded.title,
    normalized_title = excluded.normalized_title,
    outcome = excluded.outcome,
    last_processed_at = excluded.last_processed_at,
    trace_id = excluded.trace_id
`;

export const EXCLUDE_COMPANY_SQL = `
  INSERT INTO company_exclusions (normalized_company, company, excluded_at, source_key)
  VALUES (?, ?, ?, ?)
  ON CONFLICT(normalized_company) DO UPDATE SET
    source_key = COALESCE(excluded.source_key, company_exclusions.source_key)
`;

export const RECORD_PENDING_NOTION_PROJECTION_SQL = `
  INSERT INTO pending_notion_projections (source_key, job_json, status, created_at)
  VALUES (?, ?, ?, ?)
  ON CONFLICT(source_key) DO UPDATE SET
    job_json = excluded.job_json,
    status = excluded.status,
    created_at = excluded.created_at
`;

export const LIST_PENDING_NOTION_PROJECTIONS_SQL = `
  SELECT source_key, job_json, status, created_at
  FROM pending_notion_projections
  ORDER BY created_at, source_key
`;

export const MARK_NOTION_PROJECTION_COMPLETE_SQL = `
  DELETE FROM pending_notion_projections WHERE source_key = ?
`;
