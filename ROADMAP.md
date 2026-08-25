# JobFinder Roadmap

Production evolution plan. Each section is a workstream with concrete tasks.
Priority: P0 = do first, P1 = do next.

## Current state

Production runs as a Cloudflare Workflow with a D1 ledger. Structured logging with pino, Slack run reports and fatal error alerts, resilience stack (circuit breakers, retry with exponential backoff, rate limiters, semaphores), preflight schema validation, reconcile-only mode (`--reconcile-only`), location eligibility filter for remote-from-Romania, four evaluation profiles (crypto-web3, fintech-trading, senior-fullstack-react, ai-engineering), ATS-native enrichment for ashby/lever/greenhouse (behind `enableAtsEnrichment` flag), and integration tests with independent FP/FN thresholds (82 evaluate fixtures: 42 pass / 40 reject; 8 remote fixtures + 12 ATS-aware). Latest measurement: FP ≈ 7-10%, FN ≈ 5%.

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
- [ ] **Pacific-TZ overlap requirement not codified** — listings with explicit "must have N+ hours overlap with US Pacific Time" pass the eval (Romania→PT is ~10 hours, not viable; East Coast/Central/Mountain are fine). Three rule formulations were attempted in the `remote-europe-eligible` filter but each induced strictness drift on adjacent borderline-pass fixtures (conversica's "U.S. geographic markets" comp language, overstory's body country whitelist). Reject fixture pinned at `reject/loot-labs-us-pacific-tz-required.md`. Likely needs a broader filter restructure rather than a single new rule — revisit once more Pacific-TZ examples accumulate.
- [ ] **AI-platform exception in role-quality rule 8 too permissive** — listings whose responsibilities are MLOps/internal-research-platform work ("build CI/CD for model training", "build modular AI infrastructure stack — vector DBs, feature stores, model registries, observability tooling", "deploy infrastructure to support offline/online evaluation", "enable researchers to iterate quickly") pass via the AI-platform exception because the verbs are "build" rather than "operate". The exception's discriminator (build/apply/deploy-features vs operate/monitor) doesn't catch the case where what's being built is infrastructure for internal data scientists, not product features. Reject fixture pinned at `reject/trm-labs-mlops-internal-platform.md`.
- [ ] **Hype-copy + aggressive-on-call "shitshow" red flag not codified** — no filter detects the combination of (a) buzzword/grandiosity-stuffed prose from a no-name company ("elite Backend Systems Architect", "world-class, hyper-scalable", "surgical precision", "technical powerhouse", "absolute frontier") and (b) aggressive always-on operational framing baked into the role copy ("24/7/365 system uptime", on-call/incident-response stated as a routine expectation rather than an edge case). Individually each is weak/subjective; stacked, they reliably signal an immature, burnout-prone org. Distinct from `role-quality` rule 8 (which fires only when the role's *primary job* is infra-ops) and from `cheap-shop-placement` (staffing signals). NOTE: normal on-call rotation / runbooks / post-mortems / observability ownership must NOT trigger this — those are normal product-backend expectations (Alex passed Solana Labs with all of them). Likely a new stacked-signal filter (≥2 signals → fail, "when in doubt PASS", N-shot examples) so a single bit of marketing fluff doesn't over-reject. Reject fixture pinned at `reject/partyhat-hype-copy-247-oncall-shitshow.md`; safe-case pass fixture at `pass/solana-labs-mobile-backend-oncall-ok.md`. Tracked in GitHub #37. Needs more examples before drafting the rule — strictness-drift risk is high since hype language is subjective.
- [ ] **Model-training/research-heavy roles slip through the ai-engineering profile** — listings whose core is model training / distillation / quantization / model-architecture research (PyTorch + HuggingFace as core skills) keep passing despite the FAIL rule (`config/evaluation.ts:98`) and a near-exact N-shot example (`:113`). They wrap the training core in application-layer veneer (RAG, agents, evals, inference deployment) and the judge under-fires the exclusion. IMPORTANT: fine-tuning alone must keep passing (it's acceptable applied-AI); the reject line is distillation/quantization/training-from-scratch/architecture-research. Direction: broaden `:98` beyond "PhD/papers/foundation models" and add an N-shot contrast pair (both mention RAG/agents, one application-integration PASS, one training-primary FAIL). Reject fixtures pinned at `reject/zoominfo-ml-distillation-quantization-training.md` and `reject/omilia-llm-model-training-research-pytorch.md`. Tracked in GitHub #38.
- [ ] **Second-national-language requirement not codified** — a listing that requires near-native proficiency in a non-English national language (e.g. "C1 level in English and Polish") effectively restricts hiring to that country's speakers and is a hard disqualifier for a Romania-based candidate. No filter catches this today: the `remote-europe-eligible` filter sees "100% remote" + an EU country and PASSES, and `role-quality` rule 7 only fires on non-English *body text leakage*, not on a stated second-language requirement. Likely belongs in the location-eligibility filter (a stated requirement for fluency/C1/native in a national language other than English the candidate doesn't speak → FAIL). Reject fixture pinned at `reject/rtb-house-polish-c1-required.md`.

### Enrichment tests
- [ ] Create fixture file with raw job data and expected enriched output
- [ ] Test that titles get cleaned (no company name, no location suffix)
- [ ] Test that locations normalize correctly ("Remote - US/EU" → "Remote (US/EU)")
- [ ] Test that company names normalize ("ACME Corp." vs "acme" → consistent)

---

## 2. Potential Issues (Audit) (P1)

Flagged during code audit — each needs verification before fixing.

### High severity

- [ ] **URL canonicalization missed in dedup** (`src/pipeline/processUrl.ts`, `src/services/notionCache.ts`) — exact-match URL dedup against `cache.existingUrls` doesn't normalize host variants. Same Greenhouse listing slipped in twice via `boards.greenhouse.io/<co>/jobs/<id>` and `job-boards.greenhouse.io/<co>/jobs/<id>`. Need URL normalization (canonical host, trailing slash, query params) before insertion + at cache build. Recurred in the 2026-05-21 To-Review walk: Logos "Decentralised Messaging Engineer - Rust" (greenhouse id `7896004`) appeared twice — `job-boards.greenhouse.io/iftother/jobs/7896004` and `boards.greenhouse.io/iftother/jobs/7896004` — same org + same job id, host variant only.
- [ ] **Fuzzy dedup misses duplicates within a single run** (`src/pipeline/processUrl.ts`, `src/services/notionCache.ts`) — observed in production: Harvey posted two listings with identical body (different IDs `51fb953a` / `f47e1925`) and C-Serv posted two near-identical "Senior Machine Learning Engineer" / "AI Senior Machine Learning Engineer" listings (slugs `0894153033` / `C3FBCB7264`). All four landed in To-Review. Root cause: `cache.jobsByCompany` is read at dedup time (before insert), but `syncer.addTitle` only fires after insert — concurrent processing of two same-company URLs both see an empty title list at dedup time, so both pass. Need either an in-run title index updated before dedup, or post-insertion fuzzy-dedup as a second pass during reconcile. More examples from the 2026-05-21 To-Review walk: Dev.Pro posted two near-identical Team-Lead listings (`OP02126-00` "Blockchain Integration team" / `OP02126-01` "AI Agents Integration team") with effectively the same body; and Logos re-posted the same "Decentralised Messaging Engineer - Rust" role across two greenhouse org boards (`iftother` job `7896004` and `logos` job `7893070`) — a cross-board re-post that even URL canonicalization wouldn't catch, since the org slug and job id both differ.
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
- [ ] **Re-eval script duplicates `processUrl` orchestration** (`src/pipeline/processUrl.ts:77-154`, `scripts/reevaluate-to-review.ts:85-130`) — the script reimplements scrape → parse → ATS enrich + structural → body structural → evaluate inline instead of reusing the production pipeline's orchestration. The two implementations have already drifted: commit 56fd08f patched the script after `processUrl.ts` started passing `{title}` to `fetchAtsData` — the parallel script call was missed at the time of the dispatcher change, so Workable enrichment silently went dark in re-eval until the bug surfaced. The script also bypasses the concurrency stack (`jinaReaderSemaphore`/`jinaBreaker`/`withRetry` for Jina, `llmSemaphore`/`llmBreaker`/`withRetry` for the LLM call) — fine for the current 16-entry pile but capable of bursting past OpenRouter rate limits on a larger backlog. Direction: extract `evaluateUrl(url, config, opts)` into a new `src/pipeline/evaluateUrl.ts` that owns scrape → parse → ATS → structural → evaluate (wrapping every external call in the concurrency primitives) and returns a verdict struct (`{pass, reason, stage, atsSource, job, evaluation?}`). `processUrl` shrinks to: cache pre-checks → `evaluateUrl` → on reject, one shared `markAutoRejected` helper; on pass, the existing enrich/dedup/archived/applied/insert tail. `reevaluate-to-review` shrinks to: fetch pile → for each call `evaluateUrl` → report → optionally `updateJobStatus`. Net diff ≈ +80 / −70 lines; one source of truth for the eval flow. Worth folding in the same pass: extract the three duplicated `insertJob(..., "Auto-Rejected")` blocks in `processUrl` into a `markAutoRejected` helper. Rejected alternative: a `dryRun` flag on `processUrl` itself — would force "if reEvalMode, skip cache check" conditionals because re-eval entries are by definition already in `cache.existingUrls`, which is exactly the feature-flag-as-shim anti-pattern CLAUDE.md calls out. Separately tracked: the script reads env vars manually instead of going through `src/config`'s Zod-validated config (different surface, leave for a follow-up).

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

## 4. Role-Quality Filter Gaps (P1)

Surfaced during the 2026-05-01 review pass. Each gap has at least one fixture pinned in `src/pipeline/__integration__/fixtures/evaluate/reject/` so a future prompt edit can be validated with the integration suite. Order matches confidence — top entries are clear edits, lower ones need more thought.

### 4.1 DevOps / Kubernetes-primary roles (clear reject) — DONE 2026-05-01

**Evidence:** `reject/boundless-senior-infrastructure-devops.md`, `reject/yuno-platform-engineer-ai.md`. The first is "Senior Software Engineer - Infrastructure" (sounds like SWE) with body pure SRE/DevOps. The second has "AI Agent Infrastructure" branding but the responsibility list is pure messaging/IaC/observability ("when something breaks at 3am").

**Resolution:** Added role-quality rule 8 ("INFRA-OPS-AS-PRIMARY-RESPONSIBILITY"). The first attempt keyed on must-have skills (K8s + Docker + Terraform + Datadog) — that broke `pass/clickup-senior-ai-engineer.md` because AI Platform engineers list those same skills. Second attempt keys on the *responsibility verbs*: "operate", "monitor", "own deployments", "tune observability", "manage clusters" → FAIL; "build", "apply", "deploy features" → PASS. Boundless and Yuno-platform now reject; ClickUp AI Platform still passes.

### 4.2 Any Java requirement (tighten, don't loosen)

**Evidence:** No new fixture from this walk; existing `reject/okto-payments-senior-engineer-java.md` covers primary-Java. The current rule passes when "Java/.NET/C#/Scala appears only as nice-to-have, secondary, or as one of many options". Alex stated: "We also must absolutely reject any Java." This contradicts the current fallback.

**Direction:** Tighten rule 1 — required-Java in any tier (must-have, nice-to-have-but-required-on-the-team, "you'll work in a Java-heavy environment") fails. Soft mention ("we also have a few Java services") is still ambiguous; need a couple of new fixtures distinguishing "Java tangentially mentioned" (pass) vs "Java on the team and you'll touch it" (fail). Don't ship before adding those fixtures, or risk over-rejecting clean polyglot stacks.

### 4.3 Interview rounds: 4 is fine, 5 is too much

**Evidence:** Dataiku (URL `https://boards.greenhouse.io/dataiku/jobs/5963977004`, no fixture saved per user direction). Listed process: 1 recruiter call, 1 tech interview, take-home OR live coding, 2 VPs of Engineering. That is 4 sync rounds (take-home path) or 5 (live-coding path). Alex would have applied to the 4-round variant but not 5.

**Direction:** Change role-quality rule 6 threshold from "4+ synchronous rounds → FAIL" to "5+ synchronous rounds → FAIL". Update the failing example accordingly and add a 4-round pass example. Risk is low since the fail threshold is moving up (rejection rate decreases).

### 4.4 L1 / consensus / microchain protocol depth — DONE 2026-05-01

**Evidence:** `reject/linera-software-engineer-rust-l1.md` (microchain L1, Rust-only); `reject/alpen-labs-engineering-lead-l2-systems.md` (Bitcoin L2 / ZK rollups, Rust + EVM + L2 systems programming, lead role).

**Resolution:** Added role-quality rule 9 ("CORE-PROTOCOL CHAIN IMPLEMENTATION"). First attempt keyed on adjacent skills like "consensus algorithms" or "MEV awareness" — that broke `pass/d3-engineer.md` (Solana RWA, mentions consensus algorithms as a 3y experience requirement) and `pass/moonpay-engineer.md` (wallet/payment with MEV awareness desired). Second attempt keys on whether the company itself IS the chain ("first blockchain optimized for...", "scalable Bitcoin ecosystem", "high-performance OS for Ethereum") AND the role builds it ("contribute to architecture of blockchain protocols", "production-grade infrastructure for our protocol"). Application-layer roles on existing chains pass even when consensus/MEV appears in the body. The existing `feedback_stack_depth_signals` memory now matches the prompt.

### 4.5 Lever single-country with multi-continent operations language (false positive in remote filter)

**Evidence:** `pass/ats/jeeves-mexico-ats-body-multi-continent.md`. Lever metadata is unambiguous (`country=MX`, `locations=["Mexico"]`, `team="Engineering - LatAm"`). The body says "operates across 20+ countries including Brazil, Canada, Colombia, Mexico, the United Kingdom, across Europe, and the United States" — but that is *company operations / customer reach*, not *hiring scope*. The remote filter passed the listing under rule A by reading the operations language as a multi-continent team signal.

**Note:** Alex called this a pass on the basis that the operations language is enough; he's willing to apply even though hiring scope is LatAm-only. The fixture is therefore in `pass/ats/`, NOT `reject/ats/`. The gap in the prompt is whether rule A should distinguish "company operates in X" from "team distributed across X" — currently it doesn't, and the user's call here is that conflating them is acceptable. Track as a *known false-positive shape* worth revisiting if multiple Mexico-only listings start landing in To-Review.

### 4.6 Careers-index pages and "General Application" titles — DONE 2026-05-01

**Evidence:** `reject/ethena-labs-general-application.md` (title "Ethena Labs - Join the Team! General Application" — the existing `^\s*general application\b` pattern was anchored to start and missed the company-prefixed form), `reject/reown-walletconnect-careers-index.md` (URL `apply.workable.com/walletconnect/` — a careers root that lists multiple jobs, no individual posting).

**Resolution:** Extended `structuralFilter` to (a) match "general application" / "join the team" / talent-pool framing anywhere in the title (no longer anchored), and (b) reject root career-index URLs across Workable / Lever / Greenhouse / Ashby (no individual posting segment). Both fixtures now reject before reaching the LLM.

### 4.7 Hybrid buried in benefits — DONE 2026-05-01

**Evidence:** `reject/openup-senior-ai-engineer-hybrid.md`. Body says "Flexible work model (hybrid and options for remote work)" in the perks section; previous prompt missed it because the literal "hybrid" appeared away from the work-model header.

**Resolution:** Strengthened the remote filter to fail when "hybrid" is the framing default and remote is a qualified option ("options for", "with flexibility for"). Soft-pass example added for "Remote / Hybrid (Warsaw)" where hybrid and remote are parallel options — first attempt over-rejected `pass/vecten-ai-native-fullstack.md` which uses that pattern.

### 4.8 Cheap-shop staffing FPs — DONE 2026-05-01

**Evidence:** `reject/pavago-senior-backend.md`, `reject/huzzle-founding-engineer-staffing-framing.md`, `reject/via-hatchit-cryptography-engineer.md`, `reject/south-geeks-latam-staffing.md`. All four shared placement-framing language ("Our client is", "we connect [skill] with companies", "[Recruiter] is partnering with [Client]") that a naive rule would reject. But that naive rule was tried and reverted because it broke `pass/mlabs-engineer.md` and `pass/infinity-constellation-senior-fullstack.md` (legitimate consultancies / holding-cos that use the same framing).

**Resolution:** Added `cheap-shop-placement` as a 4th LLM filter in `getEvaluationFilters()`. The filter defines 8 cheap-shop signals (recruiter-placement framing, recruiter-shop self-description, recruiter-name in title, junior-bar-as-senior, comp-obscured + placement, cheap-country-only talent pool, low-code-required, contractor + foreign client hours) and fails only when ≥ 2 stack. Pavago has 4 signals, Huzzle 3, South Geeks 3, Via-Hatch 2 — all reject. MLabs has only S1 (comp disclosed cancels S5) and Infinity has none — both still pass. The filter prompt asks the LLM to list signals in its reason field for auditability ("Signals: S1, S4. Count: 2. FAIL.").

### 4.9 Remaining FP gaps to chase next (target ≤ 10%)

After the cheap-shop filter landed, FP sits at 7.5–10% across runs. The remaining steady-state FPs are not staffing-shaped:

- `reject/wayflyer-backend-engineer.md` — generic Python/Django CRUD on financial data; LLM passes via fintech-trading-infra-ts because "core financial products" pattern-matches "financial backend services". Fix: stronger negative example contrasting CRUD-on-fintech against real trading/real-time infra.
- `reject/abacus-insights-ml-engineer.md` — borderline. Healthcare AI Platform with Databricks/PyTorch/TensorFlow training; reads as deep MLE but the body lists RAG/agents/LLMs. Hard to encode without breaking ClickUp/Harvey AI Platform passes.
- `reject/tem-senior-staff-agent-platform.md` — borderline. Senior Staff Agent Platform at an energy startup; combines product-engineering verbs ("ship flagship agentic capabilities") with operational ones ("runbook/on-call ownership"). Sometimes flips run-to-run.

The errored "no function tool_call in response" cases (e.g. `lazer-engineer`, `vanta-senior-engineer` in some runs) are model-side flakes counted as wrong-direction by the suite. ROADMAP §1 tracks per-fixture flakiness measurement to separate these from real misclassifications.
