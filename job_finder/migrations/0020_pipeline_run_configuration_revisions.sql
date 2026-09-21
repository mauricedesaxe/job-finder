ALTER TABLE search_configuration_publications
ADD CONSTRAINT search_configuration_publications_revision_release_key
UNIQUE (revision_id, prompt_release_id);

ALTER TABLE pipeline_runs
ADD COLUMN configuration_revision_id CHAR(64);

CREATE FUNCTION default_legacy_orchestration_run_configuration()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.kind = 'orchestration' AND NEW.configuration_revision_id IS NULL THEN
    SELECT min(revision_id::TEXT)::CHAR(64) INTO NEW.configuration_revision_id
    FROM search_configuration_publications
    WHERE prompt_release_id = NEW.prompt_release_id
    HAVING count(*) = 1;

    IF NEW.configuration_revision_id IS NULL THEN
      RAISE EXCEPTION 'cannot infer configuration revision for prompt release %',
        NEW.prompt_release_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER legacy_orchestration_runs_use_initial_configuration
BEFORE INSERT ON pipeline_runs
FOR EACH ROW EXECUTE FUNCTION default_legacy_orchestration_run_configuration();

ALTER TABLE pipeline_runs
ADD CONSTRAINT orchestration_runs_own_configuration_revision
CHECK (configuration_revision_id IS NULL OR kind = 'orchestration') NOT VALID,
ADD CONSTRAINT pipeline_runs_configuration_publication_fk
FOREIGN KEY (configuration_revision_id, prompt_release_id)
REFERENCES search_configuration_publications(revision_id, prompt_release_id) NOT VALID;

WITH unique_release_publications AS (
  SELECT
    prompt_release_id,
    min(revision_id::TEXT)::CHAR(64) AS revision_id
  FROM search_configuration_publications
  GROUP BY prompt_release_id
  HAVING count(*) = 1
)
UPDATE pipeline_runs AS run
SET configuration_revision_id = publication.revision_id
FROM unique_release_publications AS publication
WHERE run.kind = 'orchestration'
  AND run.prompt_release_id = publication.prompt_release_id;

SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE pipeline_runs
VALIDATE CONSTRAINT orchestration_runs_own_configuration_revision,
VALIDATE CONSTRAINT pipeline_runs_configuration_publication_fk;
