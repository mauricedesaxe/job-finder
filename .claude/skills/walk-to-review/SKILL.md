---
name: walk-to-review
description: Walk Alex through the current Notion "To Review" pile one job at a time, capturing concrete pass/reject verdicts as eval-pipeline integration-test fixtures. Use this when Alex says things like "let's go through the To Review pile", "help me power through jobs", "let's improve the fixtures", or otherwise wants to triage accumulated jobs in Notion.
---

# Walk-to-Review Skill

This skill encodes the proven workflow for triaging the Notion "To Review" pile with Alex while simultaneously building the integration-test fixture set for `src/pipeline/__integration__/fixtures/evaluate/`.

## Goals

1. **Help Alex power through accumulated jobs.** He's accumulated piles before. Walking them with structured analysis is faster than him doing it solo in Notion.
2. **Improve fixtures.** Every concrete verdict turns into either a `pass/` or `reject/` fixture (when the verdict signal is in the markdown body) or just a noted skip (when the verdict relies on data the eval pipeline can't currently see).

## Two fixture tracks: body-only vs. ATS-aware

The eval suite has **two** parallel test files driven by **two** fixture trees:

- `evaluate-pipeline.test.ts` runs the full eval pipeline on body-only fixtures
  in `evaluate/pass/` and `evaluate/reject/` (top-level). The verdict signal
  must live in the Jina-scraped markdown body.
- `evaluate-ats-aware.test.ts` runs `atsStructuralFilter` + the
  `remote-europe-eligible` filter on fixtures in `evaluate/pass/ats/` and
  `evaluate/reject/ats/`. These fixtures **prepend** an `## ATS Structured
  Data` block (produced by `formatAtsBlock` in `services/ats/index.ts`) so
  the suite can test location decisions that depend on employer-set
  workplaceType / country / locations metadata.

Pick the track based on where the decisive signal actually lives:

**Save under `pass/` or `reject/` (top-level)** when the body carries the
verdict:
- Hybrid policy disclosed in the benefits section (Ledger, Invicti, OpenUp).
- Country-whitelist text in the body ("US, UK, Spain, Portugal").
- 5-round interview process listed inline.
- Stack/language requirements (Java/Spanish/Russian-text-leak/etc).
- Architectural framing (architect-only, internal devx, ML research/training).
- Aggregator/staffing-agency framing in the body.

**Save under `pass/ats/` or `reject/ats/`** with a prepended ATS block when
the verdict depends on ATS structured data (workplaceType, country,
locations) — alone or in combination with body context. Use `formatAtsBlock`
shape:

```
## ATS Structured Data (from {lever|ashby|greenhouse} API)
- Primary location: <location>
- All listed locations: <comma-separated>
- Workplace type: <Remote|Hybrid|OnSite|unspecified>
- Country (HQ): <country>
---

<unchanged Jina output below>
```

Examples already in the tree:
- `reject/ats/raya-remote-us-only-silent-body.md` — Lever country=US,
  workplaceType=Remote, body silent on geo → reject (US-only by metadata).
- `reject/ats/polymarket-onsite-ny.md` — workplaceType=OnSite hard-rejects
  regardless of body.
- `pass/ats/quicknode-remote-multi-eu.md` — locations include EU countries
  (Ireland/Portugal/Spain/UK) so the multi-country list rescues a US HQ.
- `pass/ats/contentsquare-france-ats-body-eu-broadens.md` — country=FR plus
  body language "anywhere within Europe" broadens beyond the metadata.
- `pass/ats/jeeves-mexico-ats-body-multi-continent.md` — country=MX rescued
  by body's "operates across 20+ countries including ... Europe ... United
  States" framing.

**Skip the fixture entirely** only when neither track applies:
- The Jina page is a "Job no longer available" stub or login wall.
- The verdict relies on external knowledge with no signal in body or ATS
  metadata (suspected budget, company gossip, etc.).

## The flow per job

**Hard rule: one job at a time.** After step 5 (present), **stop and wait** for Alex's verdict before doing anything else. Do not pre-fetch the next job's API or Jina body, do not summarize multiple jobs in a single turn, do not produce a "here are all N verdicts, confirm them" table. Alex reviews each job in enough depth to confirm or correct — batching turns the skill into rubber-stamping. Each job is its own conversation turn: present → wait → verdict → save/skip → mark Notion → next.

1. **Pull the pile.** `bun scripts/list-to-review.ts` lists every Notion "To Review" entry not yet covered by an existing fixture URL. Walk in order.
2. **Detect ATS source from URL** and try the public API first when possible. This is the highest-leverage filter — Lever/Ashby/Greenhouse expose `country`/`workplaceType`/`location` fields that the body often omits:
   - Ashby: `https://api.ashbyhq.com/posting-api/job-board/{org}` (returns full list; filter by id client-side)
   - Lever: `https://api.lever.co/v0/postings/{org}/{id}?mode=json`
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}`
   - Workable: no public API; fall through to Jina
3. **Use the ATS API output to decide which track the fixture goes on.** The API output is not a reason to skip — it's the input that determines whether the fixture lands in the body-only track or the ATS-aware track. `country: "MY"` + workplaceType=Remote + silent body → save under `reject/ats/`. `workplaceType: "OnSite"` → save under `reject/ats/` (hard-reject regardless of body).
4. **Fetch the body via Jina** when the API permits or doesn't apply:
   ```
   curl -s -H "Authorization: Bearer $JINA_API_KEY" -H "Accept: text/plain" -H "x-engine: browser" "https://r.jina.ai/<URL>"
   ```
   `x-engine: browser` is needed for many Ashby pages (CAPTCHA otherwise). Source `.env` to populate `JINA_API_KEY`.
5. **Present to Alex briefly.** Surface only what matters for a quick decision:
   - Company / role one-liner
   - Stack (highlight enterprise-Java/Scala/.NET/C++ as risks; TS/Node/Go/Rust/Python as fits)
   - Senior signal (years required vs Alex's 6+)
   - Comp (in body or not)
   - Location/remote signal (from body — note any conflict with API)
   - Culture flags (architect-only, internal-tooling, hybrid, in-office, agency, talent pool, 4+ rounds, language barrier)
   - Your read — pass / reject / borderline — in one sentence so he can confirm or correct fast.
6. **Get the verdict.** Alex says pass / reject / skip / torn. If torn, **skip the fixture** — don't encode an ambiguous decision.
7. **Save the fixture** in the directory chosen in step 3 — `src/pipeline/__integration__/fixtures/evaluate/{pass|reject}/<slug>.md` for body-only, `src/pipeline/__integration__/fixtures/evaluate/{pass|reject}/ats/<slug>.md` for ATS-aware (with the `formatAtsBlock` block prepended above the Jina output). Naming: `<company-slug>-<role-slug>.md`, all lowercase, hyphenated.
8. **Reflect the verdict in Notion.** Once Alex confirms a reject, move that page out of "To Review". Otherwise the same job will sit in the pile next session and (worse) get re-presented as if no decision had been made. Run at the end of each batch:

   ```
   bun scripts/mark-status.ts Rejected <url> [<url> ...]
   ```

   Pass jobs stay in "To Review" — Alex applies to them himself and updates the status when he does. Skipped/torn jobs also stay in "To Review" by default, unless he asks otherwise.

## Known signal classes (so you can pattern-match faster)

These are the reject patterns Alex has confirmed:

- **Aggregators** (Toptal, Jobgether) — already caught by `structuralFilter`.
- **Talent-pool / "General Application" titles** — already caught by `structuralFilter`.
- **Careers-index URLs** (URL has no `/j/<id>` segment, body shows multiple jobs) — not yet caught structurally; capture as a reject fixture.
- **Java / Scala / .NET / C++ stacks** — Alex calls this "enterprise BS". Reject.
- **Hybrid disclosed in benefits** ("WFH up to N days/week", weekly office events, daily lunch). Reject regardless of how product-fitting the role is.
- **Single-country whitelist** when Romania isn't on it (US-only, US/Canada, "US, UK, Spain, Portugal", Mexico, Malaysia, etc).
- **5+ interview rounds disclosed inline.** Reject (Tem-style).
- **Pure ML training/fine-tuning** roles (PyTorch + HuggingFace + distillation/fine-tuning + research). Hits the explicit profile exclusion.
- **Pure data engineering** primary (Airflow + Snowflake + dbt + 10+ years bar). Hits another explicit exclusion.
- **Internal-platform / DevEx for other teams** without product framing (Temporal Builder Tools shape). Borderline — Alex sometimes passes (Harvey) and sometimes rejects (OP Labs is a pass, but only because of the broader crypto context). Ask.
- **Staffing agencies** with no specific end-client (C-Serv, Hatch IT, Astro Sirens). Reject.
- **Foreign-language tokens leaking** into otherwise-English bodies (Russian, etc) signal CIS-origin / language barrier.
- **Architect roles** — only reject when the body shows no IC content. Pure architect with no hands-on framing → reject. Architect/lead with IC mentions → fine, present to Alex.

These are the pass patterns:

- Crypto-native company + modern polyglot stack + remote + senior.
- AI product engineering at a real product company (LLM/RAG/agents shipped to users), not internal automation.
- Small distributed startup with explicit remote-first and EU/global eligibility.
- Contract roles with clean stack and AI-first culture (Whiteshield-style).

## Commits

- Batch fixtures roughly every 6–10 reviewed jobs.
- One commit per logical batch with a `test:` prefix and a body that enumerates each fixture's reject/pass reason — these messages are the only place the fixture-class is documented.
- Keep `ROADMAP.md` open for new gaps that surface (scraper coverage, dedup, etc.) and add bullets as they come up.
- Run the full test suite (lefthook does this) before pushing.

## When to stop the session

Alex tires fast on these walkthroughs after ~25-30 jobs. Watch for "I'm getting tired", "let's stop", "let's checkpoint", or short one-word verdicts that suggest fatigue. Commit in-flight fixtures, summarize progress, propose continuing later.

## Useful scripts already in the repo

- `scripts/list-to-review.ts` — pulls all current "To Review" entries from Notion, cross-references against existing fixture URLs, lists what's left.
- `scripts/inspect-jobs.ts` — generic Notion-status dumper, useful for spot-checking other piles (Skipped, Auto-Rejected).
- `scripts/find-candidates.ts` — pulls Rejected + Skipped piles with the same URL cross-reference.
- `scripts/mark-status.ts` — moves one or more Notion pages to a given status (e.g. `bun scripts/mark-status.ts Rejected <url> ...`). Use at the end of a walk to flush the rejects out of "To Review".

## Roadmap items this skill is connected to

The most common false-positive pattern observed during these walkthroughs is location-only-in-ATS-metadata. The fix is tracked as `ROADMAP.md` Section 0 — "ATS-Native Enrichment via Public APIs" (P0). Every walkthrough should add concrete examples to that section's evidence list when relevant.
