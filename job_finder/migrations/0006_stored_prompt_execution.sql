ALTER TABLE prompt_versions
ADD COLUMN criterion TEXT,
ADD COLUMN phase TEXT;

DROP TRIGGER prompt_versions_are_immutable ON prompt_versions;

UPDATE prompt_versions
SET criterion = CASE prompt_name
    WHEN 'job-finder-filter-location-eligibility' THEN 'remote-europe-eligible'
    WHEN 'job-finder-filter-compensation' THEN 'compensation-minimum'
    WHEN 'job-finder-filter-role-quality' THEN 'role-quality'
    WHEN 'job-finder-filter-company-quality' THEN 'cheap-shop-placement'
    WHEN 'job-finder-profile-early-stage-product-engineer' THEN 'early-stage-product-engineer'
    WHEN 'job-finder-profile-applied-ai-product-engineer' THEN 'applied-ai-product-engineer'
    WHEN 'job-finder-enrichment' THEN 'enrichment'
    WHEN 'job-finder-title-deduplication' THEN 'title-deduplication'
  END,
  phase = CASE
    WHEN prompt_name LIKE 'job-finder-filter-%' THEN 'filter'
    WHEN prompt_name LIKE 'job-finder-profile-%' THEN 'profile'
    WHEN prompt_name = 'job-finder-enrichment' THEN 'enrichment'
    WHEN prompt_name = 'job-finder-title-deduplication' THEN 'deduplication'
  END;

ALTER TABLE prompt_versions
ALTER COLUMN criterion SET NOT NULL,
ALTER COLUMN phase SET NOT NULL,
ADD CONSTRAINT prompt_versions_phase CHECK (
  phase IN ('filter', 'profile', 'enrichment', 'deduplication')
);

CREATE TRIGGER prompt_versions_are_immutable
BEFORE UPDATE OR DELETE ON prompt_versions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();

ALTER TABLE prompt_release_members
ADD COLUMN position INTEGER;

DROP TRIGGER prompt_release_members_are_immutable ON prompt_release_members;

UPDATE prompt_release_members
SET position = CASE prompt_name
    WHEN 'job-finder-filter-location-eligibility' THEN 0
    WHEN 'job-finder-filter-compensation' THEN 1
    WHEN 'job-finder-filter-role-quality' THEN 2
    WHEN 'job-finder-filter-company-quality' THEN 3
    WHEN 'job-finder-profile-early-stage-product-engineer' THEN 4
    WHEN 'job-finder-profile-applied-ai-product-engineer' THEN 5
    WHEN 'job-finder-enrichment' THEN 6
    WHEN 'job-finder-title-deduplication' THEN 7
  END;

ALTER TABLE prompt_release_members
ALTER COLUMN position SET NOT NULL,
ADD CONSTRAINT prompt_release_members_position CHECK (position >= 0),
ADD CONSTRAINT prompt_release_members_release_position UNIQUE (release_id, position);

CREATE TRIGGER prompt_release_members_are_immutable
BEFORE UPDATE OR DELETE ON prompt_release_members
FOR EACH ROW EXECUTE FUNCTION reject_immutable_job_finder_row();
