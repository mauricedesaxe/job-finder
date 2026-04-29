---
name: walk-to-review
description: Walk Alex through the current Notion "To Review" pile one job at a time, capturing concrete pass/reject verdicts as eval-pipeline integration-test fixtures. Use this when Alex says things like "let's go through the To Review pile", "help me power through jobs", "let's improve the fixtures", or otherwise wants to triage accumulated jobs in Notion.
---

# Walk-to-Review Skill

This skill encodes the proven workflow for triaging the Notion "To Review" pile with Alex while simultaneously building the integration-test fixture set for `src/pipeline/__integration__/fixtures/evaluate/`.

## Goals

1. **Help Alex power through accumulated jobs.** He's accumulated piles before. Walking them with structured analysis is faster than him doing it solo in Notion.
2. **Improve fixtures.** Every concrete verdict turns into either a `pass/` or `reject/` fixture (when the verdict signal is in the markdown body) or just a noted skip (when the verdict relies on data the eval pipeline can't currently see).

## Hard rule: only fixtures whose verdict is in the body

The eval pipeline runs on the Jina-scraped markdown text. A fixture whose pass/reject decision relies on data **not** in that markdown is testing a future pipeline, not the current one — it just drags integration-test accuracy down for the wrong reason.

Skip the fixture (but still tell Alex what the API says so he can move on) when:
- The reject reason is a geo restriction only present in Ashby/Lever/Greenhouse structured fields (e.g., `isRemote=false`, `country=MY`, `workplaceType=OnSite`), with no in-body location text.
- The reject reason is external knowledge (company HQ, suspected budget) that isn't in the body.
- The page returned by Jina is a "Job no longer available" stub or login wall.

Save the fixture when the body has the actual signal:
- Hybrid policy disclosed in the benefits section (e.g., Ledger, Invicti, OpenUp).
- Country-whitelist text in the body (e.g., Dakota: "US, UK, Spain, Portugal").
- 5-round interview process listed inline.
- Stack/language requirements (Java/Spanish/Russian-text-leak/etc).
- Architectural framing (architect-only, internal devx, ML research/training).
- Aggregator/staffing-agency framing in the body.

## The flow per job

1. **Pull the pile.** `bun scripts/list-to-review.ts` lists every Notion "To Review" entry not yet covered by an existing fixture URL. Walk in order.
2. **Detect ATS source from URL** and try the public API first when possible. This is the highest-leverage filter — Lever/Ashby/Greenhouse expose `country`/`workplaceType`/`location` fields that the body often omits:
   - Ashby: `https://api.ashbyhq.com/posting-api/job-board/{org}` (returns full list; filter by id client-side)
   - Lever: `https://api.lever.co/v0/postings/{org}/{id}?mode=json`
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}`
   - Workable: no public API; fall through to Jina
3. **Quick-skip when the API closes the case.** If the API says e.g. `country: "MY"` (Malaysia) or `workplaceType: "OnSite"` and there is no other reason to look, tell Alex one line ("CoinGecko Web — Malaysia-only per Lever API, skip") and move on. **Do not save a fixture** for these per the hard rule above.
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
7. **Save the fixture** to `src/pipeline/__integration__/fixtures/evaluate/{pass|reject}/<slug>.md` using the same Jina output. Naming: `<company-slug>-<role-slug>.md`, all lowercase, hyphenated.

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

## Roadmap items this skill is connected to

The most common false-positive pattern observed during these walkthroughs is location-only-in-ATS-metadata. The fix is tracked as `ROADMAP.md` Section 0 — "ATS-Native Enrichment via Public APIs" (P0). Every walkthrough should add concrete examples to that section's evidence list when relevant.
