# JobFinder Roadmap

Production evolution plan. Each section is a workstream with concrete tasks.
Priority: P0 = do first, P1 = do next.

## Current state

CLI script (`bun src/index.ts`) deployed as a Railway cron job (every 2 days). Structured logging with pino, Slack run reports and fatal error alerts, resilience stack (circuit breakers, retry with exponential backoff, rate limiters, semaphores), preflight schema validation, reconcile-only mode (`--reconcile-only`), location eligibility filter for remote-from-Romania, four evaluation profiles (crypto-web3, fintech-trading, senior-fullstack-react, ai-engineering), ATS-native enrichment for ashby/lever/greenhouse (behind `enableAtsEnrichment` flag), and integration tests with independent FP/FN thresholds (71 evaluate fixtures: 36 pass / 35 reject; 8 remote fixtures).

---

## 1. LLM-as-a-Judge Tests (P0)

### Evaluation tests — done
- [x] 71 evaluate fixtures (36 pass, 35 reject) as markdown files in `src/pipeline/__integration__/fixtures/evaluate/`
- [x] 8 remote filter fixtures in `src/pipeline/__integration__/fixtures/remote/`
- [x] Integration tests run each fixture through the real LLM via OpenRouter
- [x] Independent FP and FN thresholds (FP ≤ 22%, FN ≤ 15%) — stricter on FP since they cost more
- [x] Misclassifications logged with reasons for debugging

### Evaluation tests — next improvements
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

- [ ] **URL canonicalization missed in dedup** (`src/pipeline/processUrl.ts`, `src/services/notionCache.ts`) — exact-match URL dedup against `cache.existingUrls` doesn't normalize host variants. Same Greenhouse listing slipped in twice via `boards.greenhouse.io/<co>/jobs/<id>` and `job-boards.greenhouse.io/<co>/jobs/<id>`. Need URL normalization (canonical host, trailing slash, query params) before insertion + at cache build.
- [ ] **Fuzzy dedup misses duplicates within a single run** (`src/pipeline/processUrl.ts`, `src/services/notionCache.ts`) — observed in production: Harvey posted two listings with identical body (different IDs `51fb953a` / `f47e1925`) and C-Serv posted two near-identical "Senior Machine Learning Engineer" / "AI Senior Machine Learning Engineer" listings (slugs `0894153033` / `C3FBCB7264`). All four landed in To-Review. Root cause: `cache.jobsByCompany` is read at dedup time (before insert), but `syncer.addTitle` only fires after insert — concurrent processing of two same-company URLs both see an empty title list at dedup time, so both pass. Need either an in-run title index updated before dedup, or post-insertion fuzzy-dedup as a second pass during reconcile.
- [ ] **RateLimiter not concurrency-safe** (`src/concurrency/rateLimiter.ts:20-30`) — `acquire()` does check-then-act (`if tokens >= 1 → tokens--`) without serialization; two concurrent callers can both pass the check. Also, `waitMs` goes negative when `this.tokens > 1` after refill
- [ ] **LLM tool_use responses not validated** (`src/pipeline/evaluate.ts:87`, `enrich.ts`, `dedup.ts:94`) — `JSON.parse(...) as <Type>` with no Zod parse; if model returns unexpected shape, data silently corrupts. Some sites have `try/catch` around the parse but no schema validation of the parsed object.
- [ ] **No request timeouts** (`src/services/http.ts`, pipeline LLM calls) — no `AbortController`/`signal` on any fetch or LLM call; a stuck request blocks the semaphore slot forever

### Medium severity

- [ ] **HTTP error response body not consumed** (`src/services/http.ts:14-18`) — on `!res.ok`, body never read before throwing; can prevent connection reuse and cause pool exhaustion
- [ ] **Only last profile rejection reason surfaced** (`src/pipeline/evaluate.ts:161`) — when all profiles reject, earlier (possibly more informative) reasons are lost
- [ ] **Description truncation at arbitrary boundary** (`src/pipeline/scrape.ts`, 8000 char limit) — `.slice(0, 8000)` can cut mid-word; feeding broken text to LLM degrades enrichment quality
- [ ] **Notion block limit silently drops content** (`src/services/notion/builders.ts`, 100 block cap) — if enriched description exceeds 100 blocks, extra blocks dropped with no warning logged
- [ ] **Dead code: `checkDuplicateUrl`** (`src/services/notion/queries.ts:5`) — exported (and re-exported in `notion/index.ts`) but never called; URL dedup is done entirely via cache

### Low severity

- [ ] **No dedup title count limit** (`src/pipeline/dedup.ts`) — sends all existing titles for a company to the LLM; 200+ titles creates a huge prompt with no chunking

---

## 3. Company Check Optimization (P1)

**Problem:** Company blocked/applied checks (`processUrl.ts:193-218`) happen *after* scrape + evaluate + enrich + dedup. With AND-ed filters and OR-ed profiles fanned out in parallel, that's roughly 7+ LLM calls per known-bad company whenever a new URL surfaces, plus the Jina scrape.

### Option A: Move check earlier (after scrape, before evaluate)

Check blocked/applied status right after `parseJobDetails()` using the raw company name. Saves all of evaluate + enrich + dedup. Challenge: raw scraped name (e.g. `"monad-foundation"` from URL path) won't exactly match cache keys (e.g. `"Monad Foundation"`). Needs a normalized/fuzzy lookup — lowercase + strip punctuation, or maintain a parallel raw-name index.

### Option B: Don't insert, just skip

Skip Notion insertion for blocked/applied companies entirely. Saves Notion writes but the URL won't be in cache, so the same URL gets re-scraped + re-evaluated every run. Only viable if combined with a local URL blocklist file.

### Option C: Pre-scrape company blocklist from URL patterns

Match company from URL path (e.g. `ashbyhq.com/company-name/`) against known blocked companies before scraping. Maximum savings (skips Jina call too) but least accurate — URL path patterns vary across ATS platforms and don't always contain the company name.

---

## 4. Quick-Eval Summary on Notion Page (P1)

**Problem:** Walking the To-Review pile in Notion is slow because each entry only shows scraped title/company/location — Alex still has to click into the page and read the body to decide. The `/walk-to-review` skill produces a punchy 5-7-line summary (stack, senior signal, comp, location/remote, culture flags, one-line read) that he found "useful and powerful" for quick triage.

**Idea:** Generate the same summary at enrichment time and store it as a Notion property (or top-of-page block) so Alex can triage straight from the Notion list view without leaving Notion.

### Sketch
- New enrichment field, e.g. `quickSummary` — bullet list with: stack one-liner, senior signal (years required), comp (in body or "not specified"), location/remote signal, culture flags (hybrid, agency, architect-only, etc.), one-line lean (pass/reject/borderline rationale).
- Add to the LLM enrichment prompt with N-shot examples mirroring the manual-walk format.
- Store as a Notion rich_text property or as a callout block at the top of the page body.
- Profile-aware: include hits/misses against the matched profile rules so the summary doubles as a transparency note.
- Could replace or complement the existing `Why Match` reason that's already surfaced.

### Open questions
- Property vs page block: properties are visible in list view (best for triage) but limited in formatting; blocks render richer but require opening the page.
- Cost: this is one more LLM call per qualified job. Could be folded into existing enrichment to avoid an extra call.
- FP risk: a confidently-wrong summary could bias Alex toward the wrong verdict. The summary should be advisory, not the verdict.

