CREATE FUNCTION require_run_budget_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  reservation_authority TEXT;
  reservation_configuration CHAR(64);
  reservation_prompt CHAR(64);
  reservation_relevance CHAR(64);
BEGIN
  IF NEW.kind <> 'orchestration' THEN
    RETURN NEW;
  END IF;

  SELECT authority_kind, configuration_revision_id, prompt_release_id,
         relevance_release_id
  INTO reservation_authority, reservation_configuration, reservation_prompt,
       reservation_relevance
  FROM execution_budget_reservations
  WHERE idempotency_key = NEW.idempotency_key
  FOR UPDATE;

  IF reservation_authority IN ('adopted', 'pinned')
     AND (NEW.configuration_revision_id, NEW.prompt_release_id,
          NEW.relevance_release_id) IS DISTINCT FROM
         (reservation_configuration, reservation_prompt, reservation_relevance) THEN
    RAISE EXCEPTION 'pipeline run differs from reserved execution authority'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER pipeline_run_requires_budget_authority
BEFORE INSERT ON pipeline_runs
FOR EACH ROW EXECUTE FUNCTION require_run_budget_authority();
