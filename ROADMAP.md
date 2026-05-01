# JobFinder Roadmap

Production evolution plan. Each section is a workstream with concrete tasks.
Priority: P0 = do first, P1 = do next.

## Current state

CLI script (`bun src/index.ts`) deployed as a Railway cron job (every 2 days). Structured logging with pino, Slack run reports and fatal error alerts, resilience stack (circuit breakers, retry with exponential backoff, rate limiters, semaphores), preflight schema validation, reconcile-only mode (`--reconcile-only`), location eligibility filter for remote-from-Romania, four evaluation profiles (crypto-web3, fintech-trading, senior-fullstack-react, ai-engineering), ATS-native enrichment for ashby/lever/greenhouse (behind `enableAtsEnrichment` flag), and integration tests with independent FP/FN thresholds (82 evaluate fixtures: 42 pass / 40 reject; 8 remote fixtures + 12 ATS-aware). Latest measurement: FP ≈ 15-17%, FN ≈ 5%.

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

- [ ] **Jina near-empty body not retried, degrades downstream LLM verdicts** (`src/concurrency/retry.ts:15-18`, `src/pipeline/scrape.ts:82-89`, `scripts/reevaluate-to-review.ts`) — `isRetryableJina` retries only on HTTP 429/500/503. A 200 with a near-empty body (just the page chrome — header link, title, no description text) succeeds at the HTTP layer; the empty markdown flows into the eval pipeline, where the LLM (correctly given empty input) cannot find a remote signal and rejects. Observed during the 2026-05-01 reevaluate-to-review dry-run on SwapRail (`https://jobs.ashbyhq.com/swaprail/3b42b9ac-57d4-4701-bb92-92a08c28e8ef`): the re-eval flagged it reject with reason "body is empty"; a fresh Jina pull immediately after returned the full posting cleanly. A non-dry-run would have moved the page to Auto-Rejected unjustly. Direction: treat a body whose markdown content (after stripping the standard `Title: / URL Source: / Markdown Content:` chrome) is shorter than ~300 chars as a retryable Jina failure inside the scrape path. Defense-in-depth: gate `reevaluate-to-review.ts` on a minimum body length before marking Auto-Rejected, so a remaining flake after retries can't poison the pile.
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

**Problem.** Walking the To-Review pile in Notion is slow because each entry only shows scraped title/company/location — Alex still has to click into the page and read the body to decide. The `/walk-to-review` skill produces a six-bullet summary (stack, senior signal, comp, location/remote, culture flags, one-line read) that he found "useful and powerful" for quick triage. We want the same summary auto-generated and surfaced at the top of every To-Review Notion page.

### Generation point — enrichment, NOT eval

Run the summary as part of `enrichJob` (`src/pipeline/enrich.ts`), AFTER the evaluation pipeline has already passed the job. Critical reasons:

- The LLM that decides pass/reject must NOT see a directional summary — that would compound a single LLM read into both the verdict and the human-facing recap.
- The summary is for human triage of jobs that already qualified. Auto-Rejected jobs don't get a summary.
- Folds into the existing enrichment LLM call as one additional tool-call field — no extra round-trip, no extra network cost beyond ~150-200 output tokens.

### Notion rendering

Render the summary as a native Notion **callout block** at the very top of the description, then the ATS block, then the Jina body:

```
[ Notion callout: quick-eval summary ]
## ATS Structured Data (from <source> API)
- Primary location: ...
- All listed locations: ...
- Workplace type: ...
- Country (HQ): ...
---

<Jina body>
```

The callout is visually distinct (colored sidebar), signals "this is the bot's read, not part of the listing", and is the first thing Alex sees on opening the page. The ATS block stays as the second block — it's still useful as audit trail and the LLM reading the page in any future reprocess (e.g. reevaluate-to-review.ts) needs it.

`src/services/notion/builders.ts` produces description blocks today via `descriptionToBlocks`. Add a sibling that emits a `callout` block with the summary content (Notion's `callout` block type accepts an icon, color, and rich_text). Front of the description blocks array.

### Summary content shape

Six bullets, fixed order, every page:

1. **Stack** — one short line listing primary languages/frameworks/infra (e.g., "TS + Node, AWS Lambda, DynamoDB, GraphQL/AppSync"). Strip filler.
2. **Senior signal** — years required vs Alex's 6+ ("5+ years required ✓" / "10+ years required — over the bar" / "not specified").
3. **Comp** — figure from the body or "not specified". Currency + range as written.
4. **Location/remote** — what the body says about workplace and geo. ATS facts already live in the ATS block below; this bullet is the body's framing in human language.
5. **Culture flags** — short comma-separated list of salient signals (agency, internal-platform, architect-only, hybrid-disclosed-in-benefits, AI-tooling-encouraged, etc.). "none observed" if nothing stands out.
6. **Read** — *factual*, not directional. Names the salient signals; does NOT say "lean pass" / "lean reject". Reason: the human eye should integrate, the LLM should observe. Example: "AI-engineering at a real product company; agency framing absent; comp not in body" — not "lean pass".

The "Read" line being factual is the FP-risk mitigation: a confidently-wrong directional summary would bias Alex toward the wrong verdict; an observation lets him judge.

### Implementation outline

1. **Schema.** Add `summary: z.string()` to the enrichment Zod schema in `src/pipeline/enrich.ts`. Single string with a stable internal format the Notion builder can parse into a callout block (or six structured fields — choose one; six fields gives stricter testing).
2. **Prompt.** Extend the existing enrichment prompt with the six-bullet contract + N-shot examples drawn from the walkthrough fixtures already in `src/pipeline/__integration__/fixtures/evaluate/`.
3. **Notion builder.** Add `summaryToCallout(summary): BlockObjectRequest` in `src/services/notion/builders.ts`. Notion API type: `callout` with `icon: { type: "emoji", emoji: "🔍" }` (or similar), `color: "gray_background"`. Front of the blocks list.
4. **Wire-up.** `processUrl.ts` already calls `enrichJob` and assigns `enriched.description`. Pass `enriched.summary` through to `insertJob` so the builder can prepend the callout.
5. **Tracker.** `TokenTracker` already wraps the enrichment LLM call — no change needed; cost shows up in the per-run report.

### Failure mode

If the summary field is missing or malformed in the LLM response, log a warning and insert the job without a callout block. Never fail enrichment over a missing summary — the verdict has already been made by that point and the body still gets through.

### Backfill

Standalone script at `scripts/regenerate-summaries.ts`, mirrors the shape of `scripts/reevaluate-to-review.ts`:

- Query all To-Review Notion pages.
- Skip pages that already have a callout block at the top (idempotent).
- Re-fetch the body, run summary-only enrichment (no re-eval), prepend the callout.
- Run after prompt iterations that change the summary contract.

Kept separate from `reevaluate-to-review.ts` because verdict prompts and summary prompts iterate independently; coupling them would force a re-eval whenever you wanted to refresh just the summaries.

### Testing

New `src/pipeline/__integration__/fixtures/summarize/` directory with the same Markdown shape as `evaluate/`, paired with a `evaluate-summarize.test.ts` that runs each fixture through the summarization step and asserts:

- All six fields are present.
- "Senior signal" mentions a year count or the literal "not specified".
- "Comp" is either a figure with currency or "not specified".
- "Read" does not contain directional language ("lean pass", "lean reject", "borderline") — schema-shaped, not text-match.
- "Culture flags" is a list (possibly empty / "none observed").

Run on every commit via lefthook? No — same as the eval integration suite, gated to `bun run test:integration` because it costs LLM tokens.

### Open questions

- **Should the summary be a single string or six structured fields in the enrichment Zod schema?** Single string is simpler to plumb but harder to validate; six fields lets the Notion builder render each bullet as its own rich_text run and lets the test suite assert per-field. Recommendation: six fields.
- **Backfill cadence.** Manual-only, or should the auto-pipeline notice a missing summary on existing To-Review entries and refill? Manual-only is simpler; auto-refill creates a "summaries drift quietly" failure mode.
- **Icon and color.** Visual choice — propose 🔍 with `gray_background`. Rebikeshed cheap.

### Out of scope

- A "Why Match" Notion property update — keep using the existing reason field for now; the callout supersedes it visually for the human.
- Recomputing the summary on every run — too expensive and it's ephemeral data.

---

## 5. Role-Quality Filter Gaps (P1)

Surfaced during the 2026-05-01 walk-to-review pass. Each gap has at least one fixture pinned in `src/pipeline/__integration__/fixtures/evaluate/reject/` so a future prompt edit can be validated with the integration suite. Order matches confidence — top entries are clear edits, lower ones need more thought.

### 5.1 DevOps / Kubernetes-primary roles (clear reject) — DONE 2026-05-01

**Evidence:** `reject/boundless-senior-infrastructure-devops.md`, `reject/yuno-platform-engineer-ai.md`. The first is "Senior Software Engineer - Infrastructure" (sounds like SWE) with body pure SRE/DevOps. The second has "AI Agent Infrastructure" branding but the responsibility list is pure messaging/IaC/observability ("when something breaks at 3am").

**Resolution:** Added role-quality rule 8 ("INFRA-OPS-AS-PRIMARY-RESPONSIBILITY"). The first attempt keyed on must-have skills (K8s + Docker + Terraform + Datadog) — that broke `pass/clickup-senior-ai-engineer.md` because AI Platform engineers list those same skills. Second attempt keys on the *responsibility verbs*: "operate", "monitor", "own deployments", "tune observability", "manage clusters" → FAIL; "build", "apply", "deploy features" → PASS. Boundless and Yuno-platform now reject; ClickUp AI Platform still passes.

### 5.2 Any Java requirement (tighten, don't loosen)

**Evidence:** No new fixture from this walk; existing `reject/okto-payments-senior-engineer-java.md` covers primary-Java. The current rule passes when "Java/.NET/C#/Scala appears only as nice-to-have, secondary, or as one of many options". Alex stated: "We also must absolutely reject any Java." This contradicts the current fallback.

**Direction:** Tighten rule 1 — required-Java in any tier (must-have, nice-to-have-but-required-on-the-team, "you'll work in a Java-heavy environment") fails. Soft mention ("we also have a few Java services") is still ambiguous; need a couple of new fixtures distinguishing "Java tangentially mentioned" (pass) vs "Java on the team and you'll touch it" (fail). Don't ship before adding those fixtures, or risk over-rejecting clean polyglot stacks.

### 5.3 Interview rounds: 4 is fine, 5 is too much

**Evidence:** Dataiku (URL `https://boards.greenhouse.io/dataiku/jobs/5963977004`, no fixture saved per user direction). Listed process: 1 recruiter call, 1 tech interview, take-home OR live coding, 2 VPs of Engineering. That is 4 sync rounds (take-home path) or 5 (live-coding path). Alex would have applied to the 4-round variant but not 5.

**Direction:** Change role-quality rule 6 threshold from "4+ synchronous rounds → FAIL" to "5+ synchronous rounds → FAIL". Update the failing example accordingly and add a 4-round pass example. Risk is low since the fail threshold is moving up (rejection rate decreases).

### 5.4 L1 / consensus / microchain protocol depth — DONE 2026-05-01

**Evidence:** `reject/linera-software-engineer-rust-l1.md` (microchain L1, Rust-only); `reject/alpen-labs-engineering-lead-l2-systems.md` (Bitcoin L2 / ZK rollups, Rust + EVM + L2 systems programming, lead role).

**Resolution:** Added role-quality rule 9 ("CORE-PROTOCOL CHAIN IMPLEMENTATION"). First attempt keyed on adjacent skills like "consensus algorithms" or "MEV awareness" — that broke `pass/d3-engineer.md` (Solana RWA, mentions consensus algorithms as a 3y experience requirement) and `pass/moonpay-engineer.md` (wallet/payment with MEV awareness desired). Second attempt keys on whether the company itself IS the chain ("first blockchain optimized for...", "scalable Bitcoin ecosystem", "high-performance OS for Ethereum") AND the role builds it ("contribute to architecture of blockchain protocols", "production-grade infrastructure for our protocol"). Application-layer roles on existing chains pass even when consensus/MEV appears in the body. The existing `feedback_stack_depth_signals` memory now matches the prompt.

### 5.5 Lever single-country with multi-continent operations language (false positive in remote filter)

**Evidence:** `pass/ats/jeeves-mexico-ats-body-multi-continent.md`. Lever metadata is unambiguous (`country=MX`, `locations=["Mexico"]`, `team="Engineering - LatAm"`). The body says "operates across 20+ countries including Brazil, Canada, Colombia, Mexico, the United Kingdom, across Europe, and the United States" — but that is *company operations / customer reach*, not *hiring scope*. The remote filter passed the listing under rule A by reading the operations language as a multi-continent team signal.

**Note:** Alex called this a pass on the basis that the operations language is enough; he's willing to apply even though hiring scope is LatAm-only. The fixture is therefore in `pass/ats/`, NOT `reject/ats/`. The gap in the prompt is whether rule A should distinguish "company operates in X" from "team distributed across X" — currently it doesn't, and the user's call here is that conflating them is acceptable. Track as a *known false-positive shape* worth revisiting if multiple Mexico-only listings start landing in To-Review.

### 5.6 Careers-index pages and "General Application" titles — DONE 2026-05-01

**Evidence:** `reject/ethena-labs-general-application.md` (title "Ethena Labs - Join the Team! General Application" — the existing `^\s*general application\b` pattern was anchored to start and missed the company-prefixed form), `reject/reown-walletconnect-careers-index.md` (URL `apply.workable.com/walletconnect/` — a careers root that lists multiple jobs, no individual posting).

**Resolution:** Extended `structuralFilter` to (a) match "general application" / "join the team" / talent-pool framing anywhere in the title (no longer anchored), and (b) reject root career-index URLs across Workable / Lever / Greenhouse / Ashby (no individual posting segment). Both fixtures now reject before reaching the LLM.

### 5.7 Hybrid buried in benefits — DONE 2026-05-01

**Evidence:** `reject/openup-senior-ai-engineer-hybrid.md`. Body says "Flexible work model (hybrid and options for remote work)" in the perks section; previous prompt missed it because the literal "hybrid" appeared away from the work-model header.

**Resolution:** Strengthened the remote filter to fail when "hybrid" is the framing default and remote is a qualified option ("options for", "with flexibility for"). Soft-pass example added for "Remote / Hybrid (Warsaw)" where hybrid and remote are parallel options — first attempt over-rejected `pass/vecten-ai-native-fullstack.md` which uses that pattern.

### 5.8 Remaining FP gaps to chase next (≤ 15% target)

Current FP rate sits at 15-17% across runs. The remaining steady-state FPs:

- `reject/wayflyer-backend-engineer.md` — generic Python/Django CRUD on financial data; LLM passes via fintech-trading-infra-ts because "core financial products" pattern-matches "financial backend services". Fix: stronger negative example contrasting CRUD-on-fintech against real trading infra.
- `reject/abacus-insights-ml-engineer.md` — borderline. Healthcare AI Platform with Databricks/PyTorch/TensorFlow training; reads as deep MLE but the body lists RAG/agents/LLMs. Hard to encode without breaking ClickUp/Harvey AI Platform passes.
- `reject/pavago-senior-backend.md`, `reject/huzzle-founding-engineer-staffing-framing.md`, `reject/via-hatchit-cryptography-engineer.md`, `reject/south-geeks-latam-staffing.md` — staffing/recruiter framing. A general "Our client is seeking" rule was tried and reverted because it broke `pass/mlabs-engineer.md` and `pass/infinity-constellation-senior-fullstack.md` (legitimate consultancies/holding-cos that use the same framing). Need either (a) cheap-shop signals stacked with placement framing (low comp, 3y bar, n8n/Zapier nice-to-haves, LATAM-only talent pool) or (b) a tighter title-pattern rule for `[Recruiter] - [Role]`.

