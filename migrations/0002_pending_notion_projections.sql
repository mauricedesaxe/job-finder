CREATE TABLE IF NOT EXISTS pending_notion_projections (
  source_key TEXT PRIMARY KEY REFERENCES processed_jobs(source_key) ON DELETE CASCADE,
  job_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pending_notion_projections_created_at
ON pending_notion_projections(created_at, source_key);
