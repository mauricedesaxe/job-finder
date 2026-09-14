ALTER TABLE evaluation_decisions DROP CONSTRAINT evaluation_decisions_stage_check;
ALTER TABLE evaluation_decisions ADD CONSTRAINT evaluation_decisions_stage_check
CHECK (decision_stage IN ('ats_structural', 'structural', 'evaluation', 'qualified', 'company_policy'));
