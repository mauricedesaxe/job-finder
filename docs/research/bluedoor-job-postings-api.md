# Research: Bluedoor Job Postings API for the discovery + scrape path

**Question:** Could the Bluedoor job-postings API replace or augment our current
`search → scrape → structuralFilter → dedup → enrich → evaluate → reconcile` pipeline,
specifically the Jina-driven discovery and scraping stages? At what integration cost, and
with what risk to evaluation trustworthiness?

**Date:** 2026-06-26. Numbers below come from live keyless probes against
`https://api.bluedoor.sh/job-postings/v1` (no API key) and the published OpenAPI spec
(`/v1/openapi.json`), not estimates.

## TL;DR

- **Augment, don't replace the eval core.** Bluedoor can replace our two Jina calls
  (`s.jina.ai` site-search + `r.jina.ai` reader-scrape) for ATS-board discovery with a single
  structured `/v1/jobs/search?include=description` call that returns the **full job body
  (`description_text`)** plus salary, decomposed location, and apply URL. That collapses two
  network-heavy, flaky stages into one and removes the per-URL scrape entirely.
- **Keep `structuralFilter`, `dedup`, `enrich`, `evaluate` exactly as they are.** Bluedoor's
  normalized fields are *not* trustworthy enough to drive verdicts (proof below: a job tagged
  `workplace_type: remote` whose body literally says "This is not a remote position"). Our
  body-text LLM eval and AND/OR composition stay the source of truth. This fits the house
  rule: the LLM/data informs decisions, composition enforces them.
- **Coverage is a superset of what we scrape today.** All 4 of our ATS boards are present
  (greenhouse 83k, lever 36k, ashby 20k, workable 915 active) and Bluedoor tracks 28 more
  providers (Workday, SmartRecruiters, iCIMS, Ashby...) = 2.4M jobs / 1.65M active, ~36k new
  per 24h. Expansion to new sources becomes a config change, not new scraper code.
- **Cost looks like zero-to-cheap.** Keyless access works right now at 10 req/s (50 burst);
  a free API key lifts it to 100 req/s (500 burst); enterprise is custom. No dollar pricing
  is published on the API surface. **Unverified:** whether sustained commercial use is truly
  free — confirm before depending on it.
- **Biggest watch-items:** (1) dedup canonicalisation — Bluedoor `source_url`/`apply_url`
  shapes differ from what `dedup.ts` canonicalises today; (2) we lose control of *what* gets
  scraped (Bluedoor decides crawl freshness — `last_crawled_at` was ~minutes old in the
  probe, good, but it's their cadence not ours).

## What the API gives us

Base: `https://api.bluedoor.sh/job-postings`. Auth: `x-api-key` header (or bearer); **works
with no auth** at a lower rate tier. Dataset (`GET /v1/stats`, live): 2,412,739 job records,
1,649,987 active, 36,387 new in 24h, 32 ATS providers, 67,118 orgs.

The endpoint that matters, `GET /v1/jobs/search`, takes rich filters that map onto our search
intent directly:

- `q` (tokenized over title + normalized title + department), `title`, `department`
- `workplace_type`, `employment_type` (enum: full_time/part_time/contract/…), `active`,
  `status`
- `location` / `location_text` / `city` / `region` / `country`
- `salary_min` / `salary_max` / `salary_exists`
- time filters: `posted_after/before`, `updated_after/before`, `changed_after/before`,
  `first_seen_after/before` — these enable **incremental sync** (only pull what changed)
- `org_ids` / `source_ids` / provider filters
- `limit` + opaque `cursor` pagination (`meta.next_cursor`)
- **`include=description`** — the flag that returns the body text

The `Job` object (from the spec, confirmed live) carries everything `enrich`/`evaluate` need:

```
title, normalized_title, department, team
workplace_type, employment_type, status, active
location_text, country, region, city
salary_raw, salary_min, salary_max, salary_currency, salary_period
source_url, apply_url
description_text          <- full body, present when include=description
provider, provider_job_key, org_id, source_id, board_id, job_id
event_fields              <- lifecycle (first_seen/last_changed/etc.)
```

Beyond search there's a lot we could grow into later: `/v1/jobs/{id}/events` and
`/v1/events/search` (job lifecycle — opened/closed/changed), webhook subscriptions
(`/v1/webhook_endpoints` + `/v1/subscriptions`) for push instead of poll, `/v1/orgs/*` for
company-level lookups, and a guarded `POST /v1/query` SQL endpoint over public views.

## Empirical findings

Live keyless probe — `GET /v1/jobs/search?q=engineer&workplace_type=remote&country=United%20States&include=description&limit=2`:

| Field | Value (first result) |
|---|---|
| title | Project Civil Engineer |
| workplace_type | **remote** |
| description_text | 4,350 chars, full body |
| body says | **"This is not a remote position."** |
| salary_min/max | 85000 / 140000 USD / year |
| city/region/country | Springfield / MO / United States |
| provider | adp_workforcenow |
| apply_url | present |

Two things this one row proves at once: (1) the API returns rich, structured, body-bearing
data keyless and fast; (2) its normalized `workplace_type` flag contradicts its own body
text. **Do not trust Bluedoor's structured flags for filtering decisions.** This is exactly
the false-positive risk our pipeline is built to absorb — so the value is the `description_text`
(better input than a Jina scrape), not the verdict-shaped fields.

ATS coverage (`GET /v1/jobs/facets?field=ats_provider`, active counts): greenhouse 83,276 ·
lever 36,399 · ashby 20,033 · workable 915. All four of our current `SEARCH_DOMAINS` are
covered. The other 28 providers (oracle_hcm 325k, icims 218k, ukg 178k, smartrecruiters 125k,
workday, …) are sources we don't scrape today.

`meta` on a keyless search returned `total_matching: 1000, total_matching_capped: true` — the
keyless tier caps reported totals at 1000. A key likely lifts this; confirm if we need exact
counts (we mostly don't — we paginate by cursor).

## Feasibility verdict

| Approach | Verdict |
|---|---|
| (a) Bluedoor as a new **search source** feeding existing `processUrl` (discovery + body via `include=description`), keeping structuralFilter→dedup→enrich→evaluate | **Start here.** Biggest win, lowest risk. Drops two flaky Jina stages; eval logic untouched. Add a `bluedoorSearch` service behind the same Semaphore→CircuitBreaker→withRetry stack. |
| (b) Use Bluedoor **lifecycle events / webhooks** to replace `prune.ts` aging heuristics with real "job closed" signals, and `changed_after` cursors for incremental runs | Buildable, clear value, but do it after (a) proves the data quality. Touches reconcile + prune. |
| (c) Trust Bluedoor's normalized fields (`workplace_type`, salary, location) to **skip or shortcut LLM eval** | **Disfavoured.** The probe shows the flags lie. Pre-filling enrich's *input* with them is fine; letting them drive a pass/reject is the false-positive trap we explicitly guard against. |
| (d) Drop Jina entirely on day one | No. Keep Jina as fallback until Bluedoor coverage/freshness is proven over a few real runs against our keyword set. |

## Integration shape (if we pursue (a))

This is the recommended *shape*, not built here.

- New `src/services/bluedoor.ts` — the only place that talks to `api.bluedoor.sh`, mirroring
  how `services/llm.ts` owns OpenRouter. Parse responses with Zod at the boundary (the spec
  is the contract; validate `Job` the moment it enters). Wire `TokenTracker`-equivalent
  request accounting if we want usage visibility.
- New env in `config/schema.ts`: `BLUEDOOR_API_KEY` (optional — keyless works), validated and
  frozen at startup like everything else.
- A `bluedoorSearch(keyword, filters)` that runs through
  `Semaphore.run(() => breaker.run(() => withRetry(...)))`, same as `searchJobs`.
- Map `Job → JobListing` so the rest of the pipeline is unchanged. `description_text` feeds
  `enrich`/`evaluate`; structured salary/location can pre-fill but never override the LLM's
  conservative location call.
- **dedup must be revisited first** (CLAUDE.md: new sources need their URL shape considered in
  `dedup.ts`). Bluedoor `apply_url`/`source_url` for the same role won't match the
  greenhouse/lever URL shapes we canonicalise today, so a job already in Notion via Jina could
  re-appear via Bluedoor. Decide canonical key (prefer `provider` + `provider_job_key`, or org
  + normalized_title) before trusting uniqueness.
- Ship with integration fixtures, same as any eval change. Because `description_text` is
  body-visible Markdown, Bluedoor jobs fixturise cleanly (the reject reason lives in the body,
  satisfying the fixture rule).

## What we cannot do (honest limits)

- **Can't trust the normalized flags.** Demonstrated. Eval still pays for itself.
- **Can't control crawl freshness.** We inherit Bluedoor's cadence. `last_crawled_at` was
  current in the probe, but a board they crawl slowly is a board we discover slowly. Jina
  site-search hits the board live; Bluedoor hits their snapshot.
- **Can't confirm commercial-use cost from the public surface.** Rate tiers are published;
  dollar terms and ToS for sustained automated use are not. Verify before depending on it.
- **Keyless totals are capped at 1000.** Fine for cursor pagination, not for analytics.
- **US-skewed.** Stats and copy are US-centric; non-US coverage (we care about Remote
  EU/Global) is unmeasured here and needs its own probe before relying on it.

## Deep-dive probes (2026-06-26): dedup, freshness, filter accuracy

Three follow-up live-probe sessions answered the data-quality questions the first pass
flagged. All keyless, against the real API.

### Dedup — Bluedoor does NOT dedup cross-posts

Bluedoor exposes **raw per-source records**. The same role posted to multiple boards appears
as multiple `job_id`s with no job-level canonical/dedup field anywhere in the schema.

- Worst case found: org **Alterra** (`board_count=23`, 23 separate Workday tenants). Of 432
  active jobs, 432 distinct `job_id` but only 366 distinct `provider_job_key` → **66 excess
  duplicate records**, each pair with a byte-identical `apply_url`.
- `job_id` is per-source (SHA1-shaped, stable, but the wrong dedup key). `(provider,
  provider_job_key)` is a decent secondary key but not globally unique. **`normalized_title`
  is null in 100% of samples** — can't group on it.
- `/v1/query` (SQL) is auth-gated (`unauthorized` keyless), so population-wide dedup counts
  need a key.

**Implication:** dedup canonical key should be `apply_url`/`source_url` — exactly what our
existing `dedup.ts` already canonicalises for ATS URLs. Revisit it for Workday/Oracle URL
shapes (tenant path present/absent) before trusting uniqueness. The within-org Workday dupes
are produced by the same mechanism that would produce cross-provider dupes, and the URL key
handles both.

### Freshness — `active=true` is NOT "open right now"

The served job read-model is a lagging snapshot; the event stream is real-time.

- `/v1/stats` self-reports `public_jobs_snapshot_lag_seconds = 58931` (**~16.4h**) while the
  source catalog and event stream lag only **68s**.
- Per-source crawl cadence is a uniform **~24h** (`last_crawled_at` + `next_crawl_after` 24h
  apart). So a record can be up to ~24h since last crawl **plus** the snapshot rebuild lag.
- **Smoking gun:** 10/10 jobs marked `job.closed` in the event stream at 07:56 UTC still
  returned `active=true, inactive_at=null` in the job API minutes later.
- Direct-URL stale-active rate: **1/60 ≈ 1.7%** for clean ATS (greenhouse/lever/ashby/workable
  all live-verifiable via their real APIs). That's a floor — real instantaneous exposure is
  ~1–2 days of lag against ~43k closes/day. Bulk providers (ADP/Oracle/iCIMS, JS-heavy) aren't
  curl-verifiable, so the rate may not generalise.
- Event types exist: `job.created/updated/closed/reopened`, fresh at 68s.

**Implication:** if open-right-now matters (it does for the apply flow), either re-validate
`apply_url` live at reconcile/apply time, or subscribe to `/v1/events/search?event_types=
job.closed` and overlay it on the snapshot. Don't rely on `active` alone. Note: our current
Jina path sees boards minutes-fresh, so this is a real regression on freshness we'd be
trading for structured data + breadth.

### Filter accuracy — defer every normalized flag to LLM body-eval

| Field | Sample | Measured | Verdict |
|---|---|---|---|
| `workplace_type=remote` | 100 | **~19% body-contradicted** (floor; up to ~46% questionable) | Don't trust; LLM body-eval |
| `workplace_type=hybrid` / `on_site` | 50 ea | 0% contradicted | Safer, still verify |
| `remote_policy` | 200 | Identical to `workplace_type` (on_site→null) | Redundant; ignore |
| `country = United States` | 230 | **~19% actually non-US** (IN/DE/ID/HU; defaults to US) | Don't trust as US-eligibility |
| `country` = explicit non-US | 30 | Correct in sample | OK when explicitly non-US |
| Remote-region (EU/Global/US-only) | 100 | **Not expressible** — no region-eligibility field, `location_text` granular ~6% | Defer entirely to LLM |
| `employment_type` enum | facets | Normalized enum usable, leaks raw passthrough values too | Usable, not exhaustive |
| `salary_min` structured | 250 | **~30% populated** | Sparse; absence ≠ no salary |
| `salary_raw` | 250 | 76.8% "present" but mostly description bleed | Unreliable; don't parse |

Verified remote-tag lies: "Project Civil Engineer … **This is not a remote position**"
(3 postings); "Call Center Sales Manager … **on-site position and requires employees to
report to work**"; remote-tagged construction laborers (forklifts, scaffolding). Country lies:
country="United States" rows located in Hyderabad IN, Frankfurt DE, Bali ID, Budapest HU.

**Implication:** this is fine for us — we already run conservative LLM location/eligibility
eval and don't trust source flags. Bluedoor's structured fields can *pre-fill* enrich input
and act as a coarse pre-filter to cut query volume, never as the verdict. Remote-region
scoping (our actual concern: Remote EU/Global/US-only) is **not expressible** in Bluedoor and
must stay in the LLM body-eval — no change from today.

### Net effect on the verdict

Approach (a) still stands and is still the recommendation, with two now-concrete must-dos:
1. **Dedup key = canonical `apply_url`/`source_url`**, extended for Workday/Oracle URL shapes,
   before any Bluedoor job is trusted as unique.
2. **Freshness guard** — live `apply_url` re-validation at reconcile, or a `job.closed` event
   overlay — because `active=true` lags real closes by up to ~1–2 days.
None of the normalized flags change our eval logic; the body-text eval we already run absorbs
the flag unreliability. (c) — trusting flags to shortcut eval — is now firmly ruled out with
numbers.

## Sources

- Landing: https://bluedoor.sh/apis/job-postings
- Reference: https://bluedoor.sh/apis/job-postings/docs/
- OpenAPI: https://api.bluedoor.sh/job-postings/v1/openapi.json
- Live probes: `/v1/stats`, `/v1/jobs/search?include=description`, `/v1/jobs/facets?field=ats_provider` (2026-06-26, keyless)
