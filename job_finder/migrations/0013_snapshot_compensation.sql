ALTER TABLE job_snapshots
  ADD COLUMN compensation_min NUMERIC,
  ADD COLUMN compensation_max NUMERIC,
  ADD COLUMN compensation_currency TEXT,
  ADD COLUMN compensation_period TEXT,
  ADD COLUMN compensation_source TEXT;
