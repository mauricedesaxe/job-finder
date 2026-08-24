CREATE TABLE IF NOT EXISTS processed_jobs (
  source_key TEXT PRIMARY KEY,
  raw_url TEXT,
  company TEXT NOT NULL,
  normalized_company TEXT NOT NULL,
  title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  outcome TEXT NOT NULL,
  first_processed_at TEXT NOT NULL,
  last_processed_at TEXT NOT NULL,
  trace_id TEXT
);

CREATE INDEX IF NOT EXISTS processed_jobs_company_title
ON processed_jobs(normalized_company, normalized_title);

CREATE INDEX IF NOT EXISTS processed_jobs_raw_url
ON processed_jobs(raw_url);

CREATE TABLE IF NOT EXISTS company_exclusions (
  normalized_company TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  excluded_at TEXT NOT NULL,
  source_key TEXT
);

CREATE TABLE IF NOT EXISTS job_ledger_migrations (
  name TEXT PRIMARY KEY,
  completed_at TEXT NOT NULL
);
