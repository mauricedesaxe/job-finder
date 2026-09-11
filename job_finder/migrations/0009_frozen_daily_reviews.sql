CREATE TABLE review_days (
  review_day DATE PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TRIGGER review_days_are_immutable
BEFORE UPDATE OR DELETE ON review_days
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
