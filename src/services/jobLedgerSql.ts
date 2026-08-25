const NOTION_PROJECTION_PAGE_SIZE = 10;

export const IMPORTED_NOTION_COMPANY_STATE_STATS_SQL = `
  SELECT
    COALESCE(SUM(CASE WHEN kind = 'blocked' THEN 1 ELSE 0 END), 0) AS blockedCompanies,
    COALESCE(SUM(CASE WHEN kind = 'recent-application' THEN 1 ELSE 0 END), 0) AS recentApplications
  FROM imported_notion_company_state
`;

export const DELETE_LEGACY_NOTION_PROCESSED_JOBS_SQL = `
  DELETE FROM processed_jobs WHERE source_key LIKE 'notion:%'
`;

export const DELETE_LEGACY_NOTION_COMPANY_EXCLUSIONS_SQL = `
  DELETE FROM company_exclusions WHERE source_key LIKE 'notion:%'
`;

export const DELETE_IMPORTED_NOTION_COMPANY_STATE_SQL = `
  DELETE FROM imported_notion_company_state
`;

export const INSERT_IMPORTED_NOTION_COMPANY_STATE_SQL = `
  INSERT INTO imported_notion_company_state (
    normalized_company, kind, company, source_key, imported_at, application_date
  ) VALUES (?, ?, ?, ?, ?, ?)
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
  FROM (
    SELECT company, excluded_at, 0 AS priority
    FROM company_exclusions
    WHERE normalized_company = ?
    UNION ALL
    SELECT company, imported_at AS excluded_at, 1 AS priority
    FROM imported_notion_company_state
    WHERE normalized_company = ? AND kind = 'blocked'
  )
  ORDER BY priority
  LIMIT 1
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
  LIMIT ${NOTION_PROJECTION_PAGE_SIZE}
`;

export const MARK_NOTION_PROJECTION_COMPLETE_SQL = `
  DELETE FROM pending_notion_projections WHERE source_key = ?
`;

export const RECORD_PENDING_REVIEW_PROJECTION_SQL = `
  INSERT INTO pending_review_projections (source_key, trace_id, created_at)
  VALUES (?, ?, ?)
  ON CONFLICT(source_key) DO UPDATE SET
    trace_id = excluded.trace_id,
    created_at = excluded.created_at
`;

export const LIST_PENDING_REVIEW_PROJECTIONS_SQL = `
  SELECT source_key, trace_id, created_at
  FROM pending_review_projections
  ORDER BY created_at, source_key
`;

export const MARK_REVIEW_PROJECTION_COMPLETE_SQL = `
  DELETE FROM pending_review_projections WHERE source_key = ?
`;
