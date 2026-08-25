CREATE TABLE IF NOT EXISTS job_finder_run_lock (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  workflow_instance_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_review_projections (
  source_key TEXT PRIMARY KEY REFERENCES processed_jobs(source_key) ON DELETE CASCADE,
  trace_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pending_review_projections_created_at
ON pending_review_projections(created_at, source_key);
