# job-finder

Automated job search, evaluation, and review. A Python pipeline discovers job
listings, filters and evaluates them against target profiles, and surfaces the
qualified results in a review queue where human feedback feeds the next
evaluation round.

```
Dagster (schedules, pools, run history)
  └─ job_finder pipeline: discover → claim → scrape → structural filter
     → evaluate (OpenRouter) → enrich → dedup → terminal decision
FastHTML review app  ← reads/writes →  PostgreSQL (the only authority)
Langfuse             ← retryable projections (never a decision input)
```

PostgreSQL owns every durable fact: jobs, immutable snapshots, pipeline runs,
immutable prompt releases, model call attempts, review queue items, and
append-only feedback. Dagster owns schedules and run history. Langfuse
receives retryable copies of evaluation traces; a Langfuse outage never changes
a job decision. `docs/architecture-rewrite.md` records why the system is
shaped this way.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- A PostgreSQL database (`JOB_FINDER_POSTGRES_DSN`); migrations apply
  automatically on first connection

## Setup

```sh
uv sync --frozen
```

### Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `JOB_FINDER_POSTGRES_DSN` | pipeline, review app | PostgreSQL authority (`public` schema) |
| `JOB_FINDER_DAGSTER_POSTGRES_DSN` | Dagster webserver/daemon | Same database, `?options=-csearch_path%3Ddagster` appended so Dagster storage lives in its own schema |
| `OPENROUTER_API_KEY` | pipeline, evaluation | LLM evaluation calls |
| `JINA_API_KEY` | pipeline | Search and scraping |
| `JOB_FINDER_IMPLEMENTATION_REF` | pipeline | Provenance ref recorded on runs |
| `JOB_FINDER_DISCOVERY_HEARTBEAT_URL` | pipeline | Better Stack heartbeat pinged after each discovery cycle (optional) |
| `JOB_FINDER_WORK_QUEUE_HEARTBEAT_URL` | pipeline | Better Stack heartbeat pinged after each queue-drain cycle (optional) |
| `JOB_FINDER_REVIEW_PASSWORD` | review app | Review login (≥12 chars) |
| `JOB_FINDER_REVIEW_SESSION_SECRET` | review app | Session signing (≥32 chars) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | projection | Presence enables the projection schedule |
| `JOB_FINDER_TEST_POSTGRES_DSN` | contracts | PostgreSQL for `pytest contracts` |

## Tasks

```sh
uv run pytest                              # unit tests
uv run pytest contracts                    # authority + Dagster contracts (needs PostgreSQL)
uv run dagster definitions validate -m job_finder.dagster
uv run python -m scripts.serve_review      # review app on :8080
uv run python -m scripts.evaluate_corpus   # full eval pipeline against real OpenRouter
uv run python -m scripts.backfill_review_queue --dry-run
                                           # preview items a backfill would create
```

Pre-commit runs ruff format, ruff check, basedpyright, and unit tests on every
commit and enforces conventional commit messages:

```sh
uv run pre-commit install --install-hooks -t pre-commit -t commit-msg
```

## Running the stack locally

```sh
docker compose up          # Dagster webserver :3000 + daemon
uv run python -m scripts.serve_review
```

## Deployment

One Docker image serves three roles on Railway; the per-service start command
decides the role:

- review app: `uv run --no-sync uvicorn scripts.serve_review:create_app --factory --host 0.0.0.0 --port $PORT`
- Dagster webserver: `uv run --no-sync dagster-webserver -h 0.0.0.0 -p $PORT -w workspace.yaml`
- Dagster daemon: `uv run --no-sync dagster-daemon run -w workspace.yaml`

`railway.toml` carries build config; healthchecks and start commands are
per-service settings. Merges to `main` cut a CalVer release tag and a GitHub
Release with generated notes.

## Schedules

- Full discovery: Wednesdays 07:00 UTC
- Work-queue drain: every 15 minutes
- Rejected-audit sample: daily 00:15 UTC (samples the just-ended day)
- Langfuse projection: every minute (self-enables when Langfuse keys are set)

## Evaluation

The evaluator ANDs the structural filter with per-criterion filter results and
ORs target profiles. False positives cost more than false negatives: promotion
requires FP ≤ 15% and FN ≤ 10% against the active prompt release. Every model
attempt is recorded in PostgreSQL (`model_call_attempts`); Langfuse gets a
retryable projection afterward.
