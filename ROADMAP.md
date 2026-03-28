# JobFinder Roadmap

Production evolution plan. Each section is a workstream with concrete tasks.
Priority: P0 = do first, P1 = do next, P2 = nice to have.

## Current state

CLI script (`bun src/index.ts`) deployed as a Railway cron job (every 2 days). No persistence between runs. No retry for individual failed jobs. No visibility into what happened unless you check Railway logs manually.

---

## 1. LLM-as-a-Judge Tests (P0)

The evaluation and enrichment prompts are the core logic of this system, but they have zero test coverage.
 We need deterministic test cases that verify LLM behavior against known inputs.

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

---

## 2. Server + Cron (P1)

Transform from a CLI script into a long-running `Bun.serve()` process that manages its own scheduling internally. No external HTTP API needed.

### Why
- Railway cron spins up a new container every run (cold start, no state)
- A persistent server can hold Redis connections, track run state, and manage BullMQ workers
- Enables retry, scheduling, and observability without external tooling

### Tasks
- [ ] Create `src/server.ts` as the new entrypoint using `Bun.serve()`
  - Health check route at `GET /health` (Railway needs this to know the service is alive)
  - No other HTTP routes
- [ ] Use `node-cron` or a simple `setInterval` for scheduling scrape runs
  - Configurable via `SCRAPE_CRON` env var (default: `0 8 */2 * *`)
- [ ] Move current `main()` logic into a `runScrape()` function that the cron triggers
- [ ] Update Dockerfile CMD to `bun run src/server.ts`
- [ ] Update package.json: `"start": "bun src/server.ts"`, keep `"scrape"` for manual CLI runs
- [ ] Railway deployment: change from cron job to web service (persistent)

---

## 3. BullMQ Job Queue (P1)

Replace the synchronous for-loop with a job queue so individual jobs can retry, be inspected, and run with controlled concurrency.

### Architecture
```
Cron trigger
  → enqueues one "scrape-run" job
    → scrape-run worker searches all keywords/domains
      → enqueues one "process-job" per URL found
        → process-job worker: scrape → evaluate → enrich → insert
          → on failure: retry with backoff (max 3 attempts)
```

### Why BullMQ
- Built on Redis, battle-tested, good TypeScript support
- Retries with configurable backoff per job
- Dead letter queue for permanently failed jobs
- Concurrency control (don't hammer Jina/Anthropic/Notion)
- Job inspection and progress tracking

### Tasks
- [ ] `bun install bullmq` (works with Bun)
- [ ] Create `src/queues/scrape-run.ts` — the top-level job that searches and enqueues
- [ ] Create `src/queues/process-job.ts` — processes a single URL through the pipeline
- [ ] Configure retry policy: 3 attempts, exponential backoff (30s, 2min, 10min)
- [ ] Configure concurrency: 3-5 concurrent process-job workers (respect rate limits)
- [ ] Add dead letter queue for jobs that fail all retries
- [ ] Wire into server.ts: cron triggers `scrape-run`, which enqueues `process-job`s
- [ ] Reconciliation runs as a separate job after all process-jobs complete (use BullMQ's `WaitingChildren` or a completion listener)

### Job data shape
```ts
// scrape-run job
{ triggeredAt: string }

// process-job
{ url: string, keyword: string, domain: string, source: string }
```

---

## 4. Redis State (P1)

Use Railway's Redis addon for persistent state between runs.

### Last-scraped tracking
- [ ] Store `lastScrapedAt` timestamp in Redis
- [ ] Before each run, check if enough time has passed (configurable interval)
- [ ] Prevents double-runs if Railway restarts the service or cron fires twice
- [ ] Key: `jobfinder:lastScrapedAt` → ISO timestamp

### URL deduplication
- [ ] Replace in-memory `seenUrls` Set with Redis Set
- [ ] Key: `jobfinder:seen-urls` → Redis SET of all processed URLs
- [ ] Check `SISMEMBER` before processing, `SADD` after successful insertion
- [ ] Optional TTL on the set (e.g., 90 days) to avoid unbounded growth
- [ ] Faster than querying Notion for every URL (current approach)

### Run history
- [ ] Store last N run summaries in Redis list
- [ ] Key: `jobfinder:runs` → list of `{ timestamp, stats, duration }`
- [ ] Useful for debugging without checking Railway logs

### Tasks
- [ ] Add Railway Redis addon, get `REDIS_URL` env var
- [ ] Create `src/services/redis.ts` — connection singleton using `Bun.redis` (not ioredis, per CLAUDE.md)
  - Note: BullMQ requires `ioredis` internally, but we can use `Bun.redis` for our own state. Check compatibility — may need `ioredis` as BullMQ peer dep regardless.
- [ ] Implement `hasSeenUrl(url)` / `markUrlSeen(url)` helpers
- [ ] Implement `getLastScrapedAt()` / `setLastScrapedAt()` helpers
- [ ] Implement `saveRunSummary(stats)` / `getRunHistory(n)` helpers

---

## 5. Configuration Cleanup (P0)

Too many hardcoded values scattered across files. Centralize everything.

### Currently hardcoded
| Value | Location | Current |
|-------|----------|---------|
| Delay between requests | config.ts | 500ms |
| Max description length | scrape.ts | 8000 chars |
| LLM model | evaluate.ts, enrich.ts | claude-haiku-4-5-20251001 |
| Eval max tokens | evaluate.ts | 256 |
| Enrich max tokens | enrich.ts | 1024 |
| HTTP retry attempts | http.ts | 3 |
| Notion block size | notion.ts | 2000 chars |
| Application lookback | notion.ts | 6 months |
| Jina base URLs | search.ts, scrape.ts | hardcoded URLs |

### Tasks
- [ ] Move all hardcoded values to `src/config.ts` with env var overrides and sensible defaults
- [ ] Add `LLM_MODEL`, `EVAL_MAX_TOKENS`, `ENRICH_MAX_TOKENS`, `SCRAPE_DELAY_MS`, `MAX_DESCRIPTION_LENGTH`, `APPLICATION_LOOKBACK_MONTHS`
- [ ] Import from config.ts everywhere instead of inline values
- [ ] Document all env vars in `.env.example` with comments

---

## 6. Resilience & Error Handling (P1)

### Timeouts
- [ ] Add explicit timeout to all fetch calls (Jina search, Jina scrape)
  - Suggested: 30s for search, 60s for scrape (some pages are slow to render)
- [ ] Add timeout to Anthropic API calls via SDK options
- [ ] Add timeout to Notion API calls

### Circuit breaker
- [ ] If >50% of recent API calls to a service fail, pause that service for N minutes
- [ ] Simple implementation: track last 10 calls per service, check failure rate before each call
- [ ] Applies to: Jina, Anthropic, Notion independently

### Error classification
- [ ] Distinguish retryable (429, 5xx, timeout) from permanent (404, 400, auth) errors
- [ ] Don't retry permanent failures
- [ ] Log error category for debugging

---

## 7. Observability (P2)

### Structured logging
- [ ] Replace console.log/error with structured logger (pino — fast, JSON output, works with Bun)
- [ ] Add fields: `runId`, `jobUrl`, `stage`, `duration`, `profileName`
- [ ] Log levels: info for pipeline progress, warn for retries/fallbacks, error for failures
- [ ] Railway captures stdout as structured logs if JSON-formatted

### Run metrics
- [ ] Track per-run: total duration, jobs found, jobs processed, API call counts, error counts
- [ ] Track per-stage: average duration, failure rate
- [ ] Store in Redis (see section 4) for internal visibility

### Cost tracking
- [ ] Track Anthropic API token usage per run (input + output tokens)
- [ ] Log estimated cost (Haiku pricing) in run summary
- [ ] Helpful for catching prompt bloat or unexpected volume spikes

---

## 8. Testing Expansion (P2)

### Integration tests
- [ ] Mock Jina API responses with realistic fixtures
- [ ] Mock Anthropic responses for pipeline integration tests
- [ ] Test full pipeline: search → scrape → evaluate → enrich → insert (mocked APIs)
- [ ] Test error paths: what happens when Jina 429s, when Anthropic returns garbage, when Notion is down

### Reconciliation tests
- [ ] Mock Notion query responses
- [ ] Test unflag-stale logic with jobs at boundary (exactly 6 months)
- [ ] Test propagation logic with mixed companies
- [ ] Test applied-companies pass

### Queue tests (after BullMQ)
- [ ] Test job retry behavior
- [ ] Test dead letter queue population
- [ ] Test concurrency limits are respected

---

## 9. Data Quality (P2)

### Job data validation
- [ ] Validate JobListing fields before insertion (URL is valid, company is non-empty, etc.)
- [ ] Reject malformed jobs with a clear log instead of inserting garbage

### LLM output validation
- [ ] Validate tool_use response shape from evaluation (pass is boolean, reason is string)
- [ ] Validate enrichment output (title/company non-empty, location is reasonable)
- [ ] Retry on malformed LLM output (up to 2 attempts)

---

## 10. Slack Run Reports (P1)

Send a summary to Slack after every run so you don't have to check Railway logs or Notion manually.

### Approach
- Use a Slack Incoming Webhook — just a `fetch` POST, no bot token or extra deps needed
- `SLACK_WEBHOOK_URL` env var — if not set, skip alerting silently (local runs won't alert)

### Message content
- [ ] Run stats: inserted, flagged, rejected, skipped, errored, unflagged, propagated
- [ ] Run duration
- [ ] Link to Notion database for quick review
- [ ] Timestamp

### Formatting
- [ ] Clean run (0 errors): simple summary, green sidebar
- [ ] Run with errors: highlight error count, orange/red sidebar
- [ ] Run with 0 new jobs: warn that scraping might be broken (all results were duplicates or rejected)
- [ ] Fatal failure (process crashed): separate error alert with stack trace

### Tasks
- [ ] Create `src/services/slack.ts` — `sendRunReport(stats, duration)` function
- [ ] Use Slack Block Kit for structured message formatting
- [ ] Call after reconciliation completes in `src/index.ts` (or after BullMQ run completes, post-migration)
- [ ] Add `SLACK_WEBHOOK_URL` to `.env.example` and Railway variables
- [ ] Also send on fatal errors (in the top-level `.catch()`)

---

## 11. ~~Preflight Check~~ (P0) ✅

Validate that the Notion database is correctly configured before scraping or reconciliation. Fail fast with clear errors instead of cryptic API failures mid-run.

### What it validates
- [x] Notion database exists and is accessible (test query)
- [x] All expected properties exist with correct types:
  - `Job Title` (title), `Company` (rich_text), `URL` (url), `Source` (select), `Keywords` (multi_select), `Date Scraped` (date), `Date Posted` (date), `Location` (rich_text), `Status` (select), `Application Date` (date)
- [x] Status select options include all `JobStatus` values: To Review, Applied, Skipped, Rejected, Company Applied, Company Blocked, Archived
- [x] Auto-create missing status options if they don't exist (Notion API supports adding select options via a dummy page insert — or use `databases.update`)
- [x] API keys are valid — not just present, but actually authenticated (make a lightweight test call)

### Where it lives
- [x] Create `src/preflight.ts` — `runPreflight(notion, databaseId)` function
- [x] Call from `main()` after `validateConfig()` but before the scrape loop
- [x] Reuse `JobStatus` type from `src/types.ts` to stay in sync automatically
- [x] On failure: log exactly which property is missing/wrong and exit with code 1

---

## Implementation order

```
Phase 1 (foundation):
  11. Preflight check
  5. Configuration cleanup
  1. LLM-as-a-judge tests

Phase 2 (architecture):
  4. Redis state (add Railway Redis)
  2. Server + cron (switch from cron job to persistent service)
  3. BullMQ job queue

Phase 3 (hardening):
  6. Resilience & error handling
  7. Observability
  10. Slack run reports
  8. Testing expansion
  9. Data quality
```
