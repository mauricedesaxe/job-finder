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

---

## 2. Potential Issues (Audit) (P1)

Flagged during code audit — each needs verification before fixing.

### High severity

- [ ] **CacheSyncer overlap** (`src/services/notionCache.ts:124-155`) — `setInterval` fires every 60s even if previous `buildNotionCache()` hasn't finished; overlapping syncs can lose in-flight `addTitle`/`addUrl` additions when `this.cache = fresh` overwrites
- [ ] **RateLimiter not concurrency-safe** (`src/concurrency/rateLimiter.ts:20-30`) — `acquire()` does check-then-act (`if tokens >= 1 → tokens--`) without serialization; two concurrent callers can both pass the check. Also, `waitMs` goes negative when `this.tokens > 1` after refill
- [ ] **LLM tool_use responses not validated** (`src/pipeline/evaluate.ts:67`, `enrich.ts:78`, `dedup.ts:73`) — `toolBlock.input as <Type>` with no Zod parse; if model returns unexpected shape, data silently corrupts
- [ ] **Dedup silent false-negative** (`src/pipeline/dedup.ts:68-71`) — missing tool_use block returns `{ isDuplicate: false }` instead of throwing; transient API issue → duplicate insertions. Inconsistent with evaluate/enrich which throw
- [ ] **No request timeouts** (`src/services/http.ts`, pipeline LLM calls) — no `AbortController`/`signal` on any fetch or Anthropic call; a stuck request blocks the semaphore slot forever

### Medium severity

- [ ] **HTTP error response body not consumed** (`src/services/http.ts:14-18`) — on `!res.ok`, body never read before throwing; can prevent connection reuse and cause pool exhaustion
- [ ] **Error objects stringified as `[object Object]`** (`src/pipeline/evaluate.ts:92`) — `${result.reason}` on a rejected Promise produces `[object Object]` instead of the error message
- [ ] **Only last profile rejection reason surfaced** (`src/pipeline/evaluate.ts:119`) — when all profiles reject, earlier (possibly more informative) reasons are lost
- [ ] **Description truncation at arbitrary boundary** (`src/pipeline/scrape.ts`, 8000 char limit) — `.slice(0, 8000)` can cut mid-word; feeding broken text to LLM degrades enrichment quality
- [ ] **Notion block limit silently drops content** (`src/services/notion/builders.ts`, 100 block cap) — if enriched description exceeds 100 blocks, extra blocks dropped with no warning logged
- [ ] **Dead code: `checkDuplicateUrl`** (`src/services/notion/queries.ts:5`) — exported but never called; URL dedup is done entirely via cache

### Low severity

- [ ] **Hardcoded model version** — all Claude calls use `"claude-haiku-4-5-20251001"`; should be a config constant or env var
- [ ] **No dedup title count limit** (`src/pipeline/dedup.ts`) — sends all existing titles for a company to Claude; 200+ titles creates a huge prompt with no chunking
- [ ] **O(n²) title merge in CacheSyncer** (`src/services/notionCache.ts:133-140`) — `existing.includes(title)` is linear scan per title; use a `Set`
- [ ] **CacheSyncer interval not cleaned up on crash** — if main pipeline throws after `syncer.start()` but before `syncer.stop()`, interval keeps firing until process exit

---

## 3. Company Check Optimization (P1)

**Problem:** Company blocked/applied checks (`processUrl.ts:143-168`) happen *after* scrape + evaluate + enrich + dedup — 4 API calls wasted per job from a known-bad company whenever a new URL surfaces.

### Option A: Move check earlier (after scrape, before evaluate)

Check blocked/applied status right after `parseJobDetails()` using the raw company name. Saves 3 Claude API calls per blocked job. Challenge: raw scraped name (e.g. `"monad-foundation"` from URL path) won't exactly match cache keys (e.g. `"Monad Foundation"`). Needs a normalized/fuzzy lookup — lowercase + strip punctuation, or maintain a parallel raw-name index.

### Option B: Don't insert, just skip

Skip Notion insertion for blocked/applied companies entirely. Saves Notion writes but the URL won't be in cache, so the same URL gets re-scraped + re-evaluated every run. Only viable if combined with a local URL blocklist file.

### Option C: Pre-scrape company blocklist from URL patterns

Match company from URL path (e.g. `ashbyhq.com/company-name/`) against known blocked companies before scraping. Maximum savings (skips Jina call too) but least accurate — URL path patterns vary across ATS platforms and don't always contain the company name.

