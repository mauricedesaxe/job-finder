# Rewrite the job finder around daily feedback

## Problem

The current app puts too much authority in tools that should remain replaceable. LangSmith owns prompt releases, accepted traces, and the review queue. Notion owns the user workflow and some pipeline policy. A LangSmith trace failure can stop job processing.

The replacement must make daily use produce evaluation evidence. Reviewing one large frozen dataset after weeks of delay does not fit how this app gets used.

## Target

The target is one Python application with two thin entry points. Dagster starts work. FastHTML renders jobs and records feedback. Domain modules own processing rules. PlanetScale PostgreSQL owns every durable fact.

The app calls OpenRouter directly. PostgreSQL records each model attempt, token count, cost, latency, prompt version, and parsed result. Langfuse receives retryable copies of traces, datasets, scores, and experiments. A Langfuse outage does not change a job decision.

PostgreSQL owns these facts:

- Jobs, immutable scraped snapshots, and exact raw URLs.
- Pipeline runs, attempts, idempotency keys, and terminal receipts.
- Immutable prompt versions and complete prompt releases.
- Model call attempts and accepted structured results.
- Daily review membership and append-only feedback.
- Company blocks and application events.
- Frozen evaluation manifests, case results, and promotion decisions.
- Pending Langfuse projections.

Dagster owns schedules, pools, sensors, and operational run history. Dagster does not own job state or evaluation provenance.

FastHTML reads only PostgreSQL domain operations. The main screen shows every new qualified job. A separate section shows a small deterministic sample of rejected jobs. The sample makes false negatives visible without filling the main review list with rejected jobs.

## Language choice

The final app uses Python 3.12. Dagster and FastHTML already require Python. A permanent TypeScript core would create a second schema and an internal process boundary for the same job model.

The current TypeScript tests remain behavior references during the port. No production request crosses between the two runtimes.

## What transfers from Chartly

The rewrite keeps these Chartly decisions:

- PostgreSQL owns domain state. Orchestration and observability remain replaceable.
- Exact input identities make retries safe.
- Feedback targets the exact immutable output shown to the user.
- Raw feedback remains separate from curated evaluation cases.
- Langfuse data can be rebuilt from PostgreSQL.
- Dagster assets call domain operations instead of containing domain logic.

The rewrite does not copy Chartly's generic artifact graph. Job Finder needs direct job, evaluation, feedback, and projection tables. The rewrite also uses ordered migrations instead of one complete schema file with a digest marker.

## Review and evaluation flow

1. Dagster discovers and processes jobs.
2. PostgreSQL stores the exact snapshot, prompt release, criterion results, and terminal decision.
3. The app creates an immutable daily review list.
4. FastHTML records `pursue`, `reject`, or `unsure` feedback against one review item.
5. Optional notes and company blocks enter the same transaction.
6. A curation action includes or excludes feedback from the next evaluation manifest.
7. Dagster runs the manifest against a candidate prompt release.
8. Promotion checks false positives and false negatives against the active release.
9. A background projection copies the manifest, run, and scores to Langfuse.

Raw feedback does not become a test answer without curation. A rejection can describe the company, the presentation, or missing information instead of an evaluator mistake.

## Migration sequence

Each phase ends with a check before the next phase starts.

1. Add the Python package and the PostgreSQL authority contract. Verify migrations, immutable rows, retry identities, and transaction rollback. Run the same contract against a PlanetScale development branch before production use.
2. Port deterministic Jina parsing, ATS adapters, structural rules, normalization, and exact URL identity. Compare every recorded fixture with TypeScript. Record intentional fixes where the old code trusts raw URL substrings.
3. Define one complete prompt release from the source-controlled bootstrap prompts. Store it in PostgreSQL, call OpenRouter directly, and record every attempt. Run the existing integration fixtures through Python.
4. Port filter, profile, enrichment, and fuzzy dedup behavior. Keep the current filter AND and profile OR rules.
5. Add FastHTML review against PostgreSQL. Verify desktop, mobile, empty, complete, unavailable, retry, and conflicting-submit states in a browser.
6. Add evaluation curation, immutable manifests, repeated trials, and Langfuse projection.
7. Add thin Dagster jobs around idempotent domain commands.
8. Delete Cloudflare Workflow, D1, LangSmith, Notion, and the TypeScript runtime.
9. Deploy the empty PostgreSQL schema and start Dagster as the only writer.

The replacement imports no existing jobs, reviews, company blocks, applications, or pending writes. New runs rebuild useful state.

## Verification gates

The rewrite is complete when all gates pass:

- Every deterministic TypeScript fixture has the same Python result.
- The complete evaluation pipeline keeps false positives at or below 15 percent.
- The complete evaluation pipeline keeps false negatives at or below 10 percent.
- No expected-negative control becomes qualified during prompt promotion.
- Repeating a run creates no duplicate job, snapshot, decision, feedback event, or projection.
- A crash after each write boundary converges on retry.
- Concurrent workers cannot claim the same work item.
- A Langfuse outage does not fail job processing.
- A projection outage leaves durable retryable work.
- Every feedback event points to the exact decision and snapshot shown in FastHTML.
- Every published evaluation manifest accounts for each selected feedback event.
- One production run completes from discovery through FastHTML review.

## Current slices

The first slice adds the Python toolchain, ordered PostgreSQL migrations, and the first authority contract. The second slice ports parsing, structural rejection, and ATS boundaries. Neither slice changes a production caller.

The live contract needs `JOB_FINDER_TEST_POSTGRES_DSN`. It creates a temporary schema, applies the migrations, runs transaction and immutability checks, and drops the schema. CI runs the contract on PostgreSQL 17. A PlanetScale development branch remains a separate pre-production gate.

Dagster, FastHTML, and Langfuse wait until the PostgreSQL contract passes. Their behavior depends on these identities and transaction rules.
