CREATE TABLE snapshot_corrections (
  snapshot_id CHAR(64) PRIMARY KEY REFERENCES job_snapshots(id),
  description TEXT,
  compensation_min NUMERIC,
  compensation_max NUMERIC,
  compensation_currency TEXT,
  compensation_period TEXT,
  compensation_source TEXT,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX snapshot_corrections_review_lookup
ON snapshot_corrections (snapshot_id);
