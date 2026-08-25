CREATE TABLE IF NOT EXISTS imported_notion_company_state (
  normalized_company TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('blocked', 'recent-application')),
  company TEXT NOT NULL,
  source_key TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  application_date TEXT,
  PRIMARY KEY (normalized_company, kind),
  CHECK (
    (kind = 'blocked' AND application_date IS NULL)
    OR (kind = 'recent-application' AND application_date IS NOT NULL)
  )
);
