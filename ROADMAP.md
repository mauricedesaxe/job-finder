# JobFinder Roadmap

Production evolution plan. Each section is a workstream with concrete tasks.
Priority: P0 = do first, P1 = do next.

## Current state

CLI script (`bun src/index.ts`) deployed as a Railway cron job (every 2 days). Structured logging with pino, Slack run reports and fatal error alerts, resilience stack (circuit breakers, retry with exponential backoff, rate limiters, semaphores), preflight schema validation, reconcile-only mode (`--reconcile-only`), location eligibility filter for remote-from-Romania, four evaluation profiles (crypto-web3, fintech-trading, senior-fullstack-react, ai-engineering), and integration tests with threshold-based accuracy (32 evaluate fixtures, 8 remote fixtures).

---

## 0. ATS-Native Enrichment via Public APIs (P0, highest leverage)

**The single most impactful false-positive fix we have identified so far.**

### Problem

The eval pipeline relies on Jina's text scrape to extract location, workplace type, and other structured signals from the job posting body. Most ATS pages (Ashby, Lever, Greenhouse) are SPAs that render structured data — location, isRemote/workplaceType, country eligibility — in sidebar widgets via JS. That metadata never reaches our markdown.

The result: roles that are explicitly "OnSite New York" or "Hybrid Chicago/SF" in the ATS structured data scrape as location-unspecified, and the geo filter waves them through. Walking the To-Review pile with Alex confirmed this is the most common false-positive class — we observed it on Polymarket (NYC OnSite), Curie (Hybrid Chicago/SF), and Ledger (hybrid policy buried in benefits, the body silent on workplace type). Conversely, body text can mislead in the other direction: Yuno's AI Engineer says "work from everywhere" but Lever's API reports `country: CO`, `allLocations: [LATAM + US]` — no Europe.

### Solution: query the ATS public API alongside (or instead of) Jina

All three of the JS-rendered ATS sources expose unauthenticated public APIs that return clean structured data:

| ATS | Endpoint | Useful fields |
|---|---|---|
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{org}` (returns full job list; filter client-side by id) | `location`, `secondaryLocations[].location`, `isRemote`, `workplaceType` (`OnSite`/`Hybrid`/`Remote`), `address.postalAddress` |
| Lever | `https://api.lever.co/v0/postings/{org}/{id}?mode=json` | `categories.location`, `categories.allLocations[]`, `categories.commitment`, `workplaceType` (`remote`/`hybrid`/`on-site`), `country` (ISO) |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}` | `location.name`, `offices[]`, `departments[]`, `content` (HTML) |
| Workable | No clean public API discovered. `apply.workable.com/api/v3/...` returns 404 unauth. Continue with Jina + body-content rules; revisit later. |

For the three that have APIs, the proposed flow inside `processUrl.ts`:

1. After URL detection (`detectSource`), branch on source.
2. For ashbyhq/lever/greenhouse: call the API, extract structured location/workplace/country fields, and either (a) prepend a `## Location\n...\n## Workplace Type\n...` block to the markdown before LLM eval, or (b) populate `JobListing.location` and a new `JobListing.workplaceType` field directly and skip the LLM enrichment for those fields.
3. For workable/other: keep current Jina-based flow.

### Open questions before implementing

- **Rate limits** — we have only hit each API once. Need to confirm: is there a per-IP limit? Per-org? What does Ashby/Lever/Greenhouse return on throttling? Need to add the same circuit-breaker / retry / rate-limiter pattern we use for Jina.
- **Reliability** — these are public-but-undocumented endpoints (Ashby's especially). What is the failure mode if the schema changes? Should we Zod-parse the response and fail safe to Jina-only on parse error?
- **Ashby endpoint shape** — `job-board/{org}` returns the whole list. For a workspace with hundreds of openings, fetching the entire list to find one ID is wasteful. Investigate whether `job-posting/{id}` works with any auth scheme (we got "Unauthorized" unauth) or whether per-org caching is the right shape.
- **Workplace type taxonomy** — Ashby uses `OnSite/Hybrid/Remote`, Lever uses `remote/hybrid/on-site`, Greenhouse has no first-class field (use offices[] heuristic). Need a normalized `JobListing.workplaceType` union and a mapper per source.
- **Body conflicts** — when the body says "work from everywhere" but the API says LATAM-only, which wins? Trust the API for hard filters (geo eligibility) since marketers write loose copy in the body. Surface both to the LLM if we keep enrichment in the loop.
- **Greenhouse content is HTML** — currently we feed Jina markdown to enrichment/eval. If we use the API content, we either need to convert HTML→markdown locally or feed HTML to the LLM (fine for eval, may cost more tokens).

### Why this is P0

Every false-positive fixture we've captured for hybrid/onsite-leaking-through (Polymarket, Curie, Ledger, OpenUp partially) would be caught at zero LLM cost the moment this lands. It is the highest-leverage single change we have identified.

---

## 1. LLM-as-a-Judge Tests (P0)

### Evaluation tests — done
- [x] 32 evaluate fixtures (23 pass, 9 reject) as markdown files in `src/pipeline/__integration__/fixtures/evaluate/`
- [x] 8 remote filter fixtures in `src/pipeline/__integration__/fixtures/remote/`
- [x] Integration tests run each fixture through real Claude API
- [x] Threshold-based accuracy (>= 75%) instead of hard per-fixture assertions
- [x] Misclassifications logged with reasons for debugging

### Evaluation tests — next improvements
- [ ] Separate false-positive and false-negative rates with independent thresholds (you may care more about false positives — wasting time on bad jobs — than false negatives)
- [ ] Run each fixture N times to measure flakiness (a fixture that passes 3/5 times is noise, not a prompt bug)
- [ ] Log which profile matched for reject fixtures that wrongly pass, to target prompt refinements

### Enrichment tests
- [ ] Create fixture file with raw job data and expected enriched output
- [ ] Test that titles get cleaned (no company name, no location suffix)
- [ ] Test that locations normalize correctly ("Remote - US/EU" → "Remote (US/EU)")
- [ ] Test that company names normalize ("ACME Corp." vs "acme" → consistent)

---

## 2. Potential Issues (Audit) (P1)

Flagged during code audit — each needs verification before fixing.

### High severity

- [ ] **Ashby metadata not captured by scraper** (`src/pipeline/scrape.ts`) — Ashby listings have structured fields (location type, city, hybrid/remote tags) that render via JS metadata. Jina's text scrape returns the role body but not these fields. Result: jobs that are explicitly "Hybrid in Chicago/SF" in Ashby's UI scrape as location-unspecified, the eval can't see the geo signal, and they land in To-Review. Observed on: Curie (Hybrid Chicago/SF), Ledger (hybrid policy disclosed only in benefits paragraph). Likely needs a separate Ashby JSON fetch or a different Jina engine.
- [ ] **URL canonicalization missed in dedup** (`src/pipeline/processUrl.ts`, `src/services/notionCache.ts`) — exact-match URL dedup against `cache.existingUrls` doesn't normalize host variants. Same Greenhouse listing slipped in twice via `boards.greenhouse.io/<co>/jobs/<id>` and `job-boards.greenhouse.io/<co>/jobs/<id>`. Need URL normalization (canonical host, trailing slash, query params) before insertion + at cache build.
- [ ] **Fuzzy dedup misses duplicates within a single run** (`src/pipeline/processUrl.ts:135-158`, `src/services/notionCache.ts`) — observed in production: Harvey posted two listings with identical body (different IDs `51fb953a` / `f47e1925`) and C-Serv posted two near-identical "Senior Machine Learning Engineer" / "AI Senior Machine Learning Engineer" listings (slugs `0894153033` / `C3FBCB7264`). All four landed in To-Review. Likely root cause: `cache.jobsByCompany` only contains pre-run state; concurrent processing of two same-company URLs in one run sees an empty title list at dedup time, so both pass. Need either an in-run title index updated as jobs are inserted, or post-insertion fuzzy-dedup as a second pass during reconcile.
- [ ] **CacheSyncer overlap** (`src/services/notionCache.ts:124-155`) — `setInterval` fires every 60s even if previous `buildNotionCache()` hasn't finished; overlapping syncs can lose in-flight `addTitle`/`addUrl` additions when `this.cache = fresh` overwrites
- [ ] **RateLimiter not concurrency-safe** (`src/concurrency/rateLimiter.ts:20-30`) — `acquire()` does check-then-act (`if tokens >= 1 → tokens--`) without serialization; two concurrent callers can both pass the check. Also, `waitMs` goes negative when `this.tokens > 1` after refill
- [ ] **LLM tool_use responses not validated** (`src/pipeline/evaluate.ts:67`, `enrich.ts:78`, `dedup.ts:73`) — `toolBlock.input as <Type>` with no Zod parse; if model returns unexpected shape, data silently corrupts
- [ ] **Dedup silent false-negative** (`src/pipeline/dedup.ts:68-71`) — missing tool_use block returns `{ isDuplicate: false }` instead of throwing; transient API issue → duplicate insertions. Inconsistent with evaluate/enrich which throw
- [ ] **No request timeouts** (`src/services/http.ts`, pipeline LLM calls) — no `AbortController`/`signal` on any fetch or LLM call; a stuck request blocks the semaphore slot forever

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

