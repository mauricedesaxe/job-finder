---
name: walk-manual-rejects
description: Walk Alex through the most recent manually-rejected jobs in Notion (Status=Rejected with Profile/Location filled — eval said pass, Alex rejected after reviewing; distinct from Status=Auto-Rejected, where the pipeline rejected on its own) one at a time, mining each confirmed false positive for body- or ATS-visible signal and turning it into a `reject/` integration-test fixture. Use this when Alex says things like "let's mine my recent manual rejects", "let's go through the latest manual rejects", "let's harvest the manual-reject pile for fixtures", or otherwise wants to convert manual rejections into eval-pipeline fixtures.
---

# Walk-Manual-Rejects Skill

Sibling of `/walk-to-review`. Where that skill triages jobs whose verdict is not yet known, **this skill mines jobs Alex has already manually rejected** — Status=Rejected with `Profile`/`Location` filled, distinct from `Status=Auto-Rejected` (pipeline rejected on its own, Alex never saw the job). Every entry is a known eval false positive, and the analytical work is identifying *why* and whether the reason is in scope of the eval pipeline.

## Goals

1. **Convert confirmed FPs into reject fixtures.** User-rejected jobs (Status=Rejected with `Profile`/`Location` filled) are the highest-signal `reject/` material — the pipeline marked them pass and Alex disagreed. The FP-over-FN bias makes these especially valuable.
2. **Don't fabricate fixtures.** Not every user-rejection is in scope of the eval pipeline. If the reject reason is external (comp, vibe, company gossip), skip — the eval's pass was correct.
3. **Pattern-match against known signal classes.** The catalog is in `/walk-to-review` and the memory; reuse it. New patterns become roadmap items.

## Two fixture tracks: body-only vs. ATS-aware

Same as `/walk-to-review`. Recap:

- `evaluate-pipeline.test.ts` runs the full eval pipeline on body-only fixtures
  in `evaluate/pass/` and `evaluate/reject/`. The verdict signal must live in
  the Jina-scraped markdown body.
- `evaluate-ats-aware.test.ts` runs `atsStructuralFilter` + the
  `remote-europe-eligible` filter on fixtures in `evaluate/{pass,reject}/ats/`.
  These prepend an `## ATS Structured Data` block (produced by `formatAtsBlock`
  in `services/ats/index.ts`) so the suite can test decisions that depend on
  workplaceType / country / locations metadata.

Pick the track based on where the decisive signal lives. Examples already in the tree are listed in `/walk-to-review`.

## The four-way classification per job

This is the analytical core of the skill. Every reviewed job lands in one of:

1. **In-scope, body-visible** → save under `reject/<slug>.md`. The body shows the reject reason (hybrid disclosed in benefits, country whitelist text, enterprise-Java stack, 5-round process, agency framing, architect-only with no IC, etc).
2. **In-scope, ATS-visible only** → save under `reject/ats/<slug>.md` with the prepended `formatAtsBlock`. The reject reason lives in workplaceType / country / locations metadata that the body omits.
3. **In-scope but signal is invisible** → **skip the fixture**. The reject is a real pipeline gap, but no fixture can capture it from body or ATS data alone (e.g. comp inferred from external knowledge, suspected budget, headcount rumor). Note it for ROADMAP if it's a new gap.
4. **Out of scope** → **skip the fixture**. The eval's pass was correct under its current contract; Alex rejected for reasons the pipeline isn't supposed to catch (personal fit, found a better lead, comp lower than asked-but-not-disclosed, "didn't feel right"). Don't fabricate a reject signal.

If you find yourself wanting to write a fixture for case 3 or 4, **stop**. Surface it to Alex as "I don't see a body or ATS signal here — skip?" and trust his answer.

## The flow per job

**Hard rule: one job at a time.** Same as `/walk-to-review`. After step 5 (present), stop and wait for Alex's classification before doing anything else. Do not pre-fetch the next job, do not summarize multiple jobs in one turn, do not produce a "here are all N classifications" table. Each job is its own conversation turn: present → wait → classification → save/skip → next.

1. **Pull the pile.** `bun scripts/list-recent-manual-rejects.ts [N]` lists the most recent user-rejected entries (Profile/Location filled, eval said pass, Alex rejected) that don't yet have a fixture. Default N=20. Walk in order — most recent first, since recent rejects most likely reflect the current prompt set.
2. **Detect ATS source from URL** and try the public API first. Same as `/walk-to-review`:
   - Ashby: `https://api.ashbyhq.com/posting-api/job-board/{org}` (returns full list; filter by id client-side)
   - Lever: `https://api.lever.co/v0/postings/{org}/{id}?mode=json`
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}`
   - Workable: no public API; fall through to Jina
3. **Use ATS API output to anticipate the track.** `country: "MY"` + workplaceType=Remote + silent body → likely `reject/ats/`. `workplaceType: "OnSite"` → `reject/ats/`. Body-rich rejects → `reject/`.
4. **Fetch the body via Jina** when API permits or doesn't apply:
   ```
   curl -s -H "Authorization: Bearer $JINA_API_KEY" -H "Accept: text/plain" -H "x-engine: browser" "https://r.jina.ai/<URL>"
   ```
   `x-engine: browser` is needed for many Ashby pages. Source `.env` to populate `JINA_API_KEY`.
5. **Present to Alex briefly.** The verdict is *reject* — you're proposing the **signal class and track**, not the verdict:
   - Company / role one-liner
   - Stack
   - Senior signal (years vs Alex's 6+)
   - Comp (if disclosed)
   - Location/remote signal (body vs API; flag conflicts)
   - Culture flags
   - **Your read of the reject reason**, mapped to one of the four classifications above. One sentence, e.g. "Hybrid 2 days/week disclosed in benefits → in-scope body-visible → `reject/`" or "Body says nothing about geo, ATS shows country=US workplaceType=Remote → in-scope ATS-visible → `reject/ats/`" or "I don't see a pipeline-relevant reject signal here — possibly out of scope?"
6. **Get the classification.** Alex confirms or corrects. If ambiguous, **skip** — don't encode a guess.
7. **Save the fixture** in the chosen directory, or skip if classification is 3 or 4. Naming: `<company-slug>-<role-slug>.md`, lowercase, hyphenated. ATS-aware fixtures get the `formatAtsBlock` prepended above the Jina output.
8. **No Notion update.** These jobs are already `Rejected` — nothing to flush. Move on.

## Why this is different from /walk-to-review

- **Verdict is given.** You're not asking pass/reject/borderline — every job is a reject. Don't waste turns re-deciding; spend them classifying the signal.
- **No Notion mutation.** Skip the `mark-status.ts` step.
- **Higher rate of "skip" outcomes is fine.** Many manual rejects are out-of-scope or invisible to body/ATS. A walk that produces 3 fixtures from 20 rejects is still successful — you've validated that the other 17 are not pipeline gaps.
- **New signal classes go to ROADMAP.** When you hit a case-3 (in-scope but invisible) repeatedly, that's a roadmap entry, not a fixture. Note it; don't force-fit.

## Known signal classes

Same catalog as `/walk-to-review`. Reproduced here for fast reference (see that skill for nuance):

**Reject patterns:**
- Aggregators (Toptal, Jobgether) — usually caught by `structuralFilter` already; if one slipped through, that's a structural gap.
- Talent-pool / "General Application" titles — same as above.
- Careers-index URLs without `/j/<id>` segment — capture as reject fixture.
- Enterprise stacks (Java / Scala / .NET / C++).
- Hybrid disclosed in benefits ("WFH up to N days/week", weekly office events, daily lunch).
- Single-country whitelist excluding Romania (US-only, US/Canada, "US, UK, Spain, Portugal", Mexico, Malaysia).
- 5+ interview rounds disclosed inline.
- Pure ML training/fine-tuning (PyTorch + HuggingFace + distillation/fine-tuning + research).
- Pure data engineering primary (Airflow + Snowflake + dbt + 10+ years bar).
- Internal-platform / DevEx without product framing — borderline; ask.
- Staffing agencies with no specific end-client.
- Foreign-language tokens leaking into otherwise-English bodies.
- Architect roles with no IC content.
- Cheap-country presence overrides EU rescue; token Spain doesn't save a listing skewed to cheap non-EU.
- Core-protocol depth signals (C++/RPC-nodes/L1/microchain/consensus).
- Supabase as foundational data layer at founding/senior eng — yellow flag, reject when stacked with other smells.

**Pass patterns** (relevant when classifying a reject as "out of scope" — if the job *should* have passed, the reject is out of scope):
- Crypto-native company + modern polyglot stack + remote + senior.
- AI product engineering at a real product company.
- Small distributed startup with explicit remote-first and EU/global eligibility.
- Contract roles with clean stack and AI-first culture.

## Commits

- Batch fixtures roughly every 6–10 reviewed jobs.
- One commit per logical batch with a `test:` prefix and a body that enumerates each fixture's reject reason — the only place the fixture-class is documented.
- If the walk surfaces a new pipeline gap (case 3) or a structural-filter miss (an aggregator that slipped through), open a ROADMAP bullet in the same batch.
- Run the full test suite (lefthook does this) before pushing.

## When to stop the session

Same fatigue dynamic as `/walk-to-review`. Watch for "I'm getting tired", "let's stop", "let's checkpoint", or short one-word answers. Commit in-flight fixtures, summarize progress, propose continuing later.

## Useful scripts

- `scripts/list-recent-manual-rejects.ts` — pulls the most recent user-rejected (Profile/Location filled) Notion entries that don't yet have a fixture. Default N=20.
- `scripts/find-candidates.ts` — broader pile dump (Rejected + Skipped, with FP/auto-reject split). Useful when you want more context than `list-recent-manual-rejects.ts`.
- `scripts/inspect-jobs.ts` — generic Notion-status dumper.
