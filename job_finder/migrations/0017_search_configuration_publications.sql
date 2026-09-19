CREATE TABLE search_configuration_publications (
  revision_id CHAR(64) PRIMARY KEY REFERENCES search_configuration_revisions(id),
  prompt_release_id CHAR(64) NOT NULL REFERENCES prompt_releases(id),
  published_at TIMESTAMPTZ NOT NULL,
  published_by TEXT NOT NULL CHECK (published_by <> '')
);

CREATE TRIGGER search_configuration_publications_are_immutable
BEFORE UPDATE OR DELETE ON search_configuration_publications
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
