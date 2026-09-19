CREATE TABLE evaluation_manifest_requests (
  idempotency_key TEXT PRIMARY KEY,
  manifest_id CHAR(64) NOT NULL REFERENCES evaluation_manifests(id),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TRIGGER evaluation_manifest_requests_are_immutable
BEFORE UPDATE OR DELETE ON evaluation_manifest_requests
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
