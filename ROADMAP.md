# JobFinder Roadmap

Production evolution plan. Each section is a workstream with concrete tasks.
Priority: P0 = do first, P1 = do next.

## Current state

CLI script (`bun src/index.ts`) deployed as a Railway cron job (every 2 days). Structured logging with pino, Slack run reports and fatal error alerts, resilience stack (circuit breakers, retry with exponential backoff, rate limiters, semaphores), preflight schema validation, reconcile-only mode (`--reconcile-only`), and a location eligibility filter for remote-from-Romania.

---

## 1. LLM-as-a-Judge Tests (P0)

The evaluation and enrichment prompts are the core logic of this system, but they have zero test coverage. We need deterministic test cases that verify LLM behavior against known inputs.

### Evaluation tests
- [ ] Create a test fixture file (`src/pipeline/__tests__/fixtures/evaluation-cases.json`) with ~20 job listings and expected pass/fail per profile
  - 5-6 clear passes (remote, senior, TS, crypto)
  - 5-6 clear fails (on-site, junior, marketing, C++ HFT)
  - 5-6 edge cases (ambiguous timezone, "senior" not in title but implied, Go-only stack)
  - 2-3 per additional profile (engineering-lead, fintech-trading)
- [ ] Write test that runs each fixture through `evaluateJob()` against real Claude API
- [ ] Assert pass/fail matches expected result; log the reason for manual review
- [ ] Track pass rate — if LLM disagrees on >15% of fixtures, something drifted
- [ ] Tag these tests separately (`bun test --grep llm`) since they're slow and cost money

### Enrichment tests
- [ ] Create fixture file with raw job data and expected enriched output
- [ ] Test that titles get cleaned (no company name, no location suffix)
- [ ] Test that locations normalize correctly ("Remote - US/EU" → "Remote (US/EU)")
- [ ] Test that company names normalize ("ACME Corp." vs "acme" → consistent)

### Guard against prompt regression
- [ ] When profile.ts prompts change, LLM tests must still pass
- [ ] Consider snapshotting LLM responses for known fixtures (not as assertions, but as a diff review tool)

