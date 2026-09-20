DO $$
DECLARE
  initial_revision_id CONSTANT CHAR(64) :=
    '621346c249608e7d8766902c2cd9fbcfb83ac687f58de8a8fdb7f81980a14099';
  initial_prompt_release_id CHAR(64);
BEGIN
  SELECT prompt_release_id INTO initial_prompt_release_id
  FROM search_configuration_publications
  WHERE revision_id = initial_revision_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'initial search configuration publication % is missing', initial_revision_id;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pipeline_runs
    WHERE kind = 'orchestration'
      AND prompt_release_id IS DISTINCT FROM initial_prompt_release_id
  ) THEN
    RAISE EXCEPTION 'historical orchestration runs do not use the initial prompt release';
  END IF;
END;
$$;

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
    NEW.configuration_revision_id :=
      '621346c249608e7d8766902c2cd9fbcfb83ac687f58de8a8fdb7f81980a14099';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER legacy_orchestration_runs_use_initial_configuration
BEFORE INSERT ON pipeline_runs
FOR EACH ROW EXECUTE FUNCTION default_legacy_orchestration_run_configuration();

ALTER TABLE pipeline_runs
ADD CONSTRAINT orchestration_runs_own_configuration_revision
CHECK ((kind = 'orchestration') = (configuration_revision_id IS NOT NULL)) NOT VALID,
ADD CONSTRAINT pipeline_runs_configuration_publication_fk
FOREIGN KEY (configuration_revision_id, prompt_release_id)
REFERENCES search_configuration_publications(revision_id, prompt_release_id) NOT VALID;

UPDATE pipeline_runs
SET configuration_revision_id =
  '621346c249608e7d8766902c2cd9fbcfb83ac687f58de8a8fdb7f81980a14099'
WHERE kind = 'orchestration';

SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE pipeline_runs
VALIDATE CONSTRAINT orchestration_runs_own_configuration_revision,
VALIDATE CONSTRAINT pipeline_runs_configuration_publication_fk;
