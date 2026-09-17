# job-finder

Job Finder searches job boards, evaluates each listing against your criteria,
and puts the strongest matches in a private review queue. It uses Dagster to
run the search pipeline, PostgreSQL to store results, Jina to find and scrape
listings, and OpenRouter to evaluate them.

[![I made my job search even easier with AI (I improved my job finder)](https://i.ytimg.com/vi/bO7vzA0xbWg/hqdefault.jpg)](https://youtu.be/bO7vzA0xbWg)

[Watch the Job Finder demo on YouTube](https://youtu.be/bO7vzA0xbWg).

This README has two setup paths:

- [Run Job Finder for yourself](#run-job-finder-for-yourself) if you want job
  results as soon as possible.
- [Contribute to Job Finder](#contribute-to-job-finder) if you want a local
  development environment.

## Run Job Finder for yourself

The steps below run the full application on your computer. The default search
and evaluation criteria target senior product engineering and applied AI roles
that can be worked remotely from Europe. Change those defaults before your
first search if they do not fit you.

### 1. Install the requirements

Install:

- [Git](https://git-scm.com/downloads)
- [Python 3.12](https://www.python.org/downloads/)
- [uv 0.12.9 or newer](https://docs.astral.sh/uv/getting-started/installation/)
- [Docker](https://docs.docker.com/get-started/get-docker/)
- A [Jina API key](https://jina.ai/api-dashboard/)
- An [OpenRouter API key](https://openrouter.ai/settings/keys) with enough
  credit for model calls

Clone the repository and install its locked dependencies:

```sh
git clone https://github.com/mauricedesaxe/job-finder.git
cd job-finder
uv sync --frozen
```

### 2. Start PostgreSQL

If you already have PostgreSQL, create a database and a `dagster` schema, then
use your connection details in the next step. Otherwise, start PostgreSQL 17
in Docker:

```sh
docker run --name job-finder-postgres --detach \
  --env POSTGRES_PASSWORD=postgres \
  --env POSTGRES_DB=job_finder \
  --publish 5432:5432 \
  postgres:17

until docker exec job-finder-postgres pg_isready -U postgres -d job_finder; do sleep 1; done
docker exec job-finder-postgres \
  psql -U postgres -d job_finder -c 'CREATE SCHEMA IF NOT EXISTS dagster'
```

Use `docker start job-finder-postgres` after a restart. The container keeps its
database until you remove the container.

### 3. Configure the application

Create your local environment file:

```sh
cp .env.example .env
```

Open `.env` and replace these values:

- `JINA_API_KEY`
- `OPENROUTER_API_KEY`
- `JOB_FINDER_REVIEW_PASSWORD`, with at least 12 characters
- `JOB_FINDER_REVIEW_SESSION_SECRET`, with at least 32 random characters

Generate a session secret with:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The sample database URLs match the Docker container from step 2. If you use
another PostgreSQL database, update `JOB_FINDER_POSTGRES_DSN` and
`JOB_FINDER_DAGSTER_POSTGRES_DSN`. Keep the encoded `dagster` search path at
the end of the Dagster URL.

Load `.env` in every terminal that runs Job Finder:

```sh
set -a
source .env
set +a
```

### 4. Set your job criteria

The repository includes the original author's criteria. Review these files
before spending API credit:

- `job_finder/discovery/catalog.py` defines search terms and job-board domains.
- `job_finder/evaluation/prompts.py` defines location, compensation, role, and
  target-profile criteria.
- `job_finder/jobs/structural_filter.py` rejects titles that are outside the
  search scope before model evaluation.

Edit the plain-text lists and prompts in those files. Then initialize the
database and save the current prompt release:

```sh
uv run python -m scripts.bootstrap_postgres_prompts
```

The command prints the prompt release name and ID when setup succeeds.

### 5. Start Job Finder

Start Dagster in one terminal:

```sh
set -a; source .env; set +a
DAGSTER_HOME="$PWD" uv run dagster dev -w workspace.yaml
```

Start the review app in a second terminal:

```sh
set -a; source .env; set +a
uv run uvicorn scripts.serve_review:create_app --factory \
  --host 127.0.0.1 --port 8080
```

Open:

- Dagster: <http://localhost:3000>
- Review queue: <http://localhost:8080>

Log in to the review queue with `JOB_FINDER_REVIEW_PASSWORD`.

### 6. Run your first search

Open Dagster, select **Jobs**, select **job_finder**, and launch a run. The run
discovers listings, evaluates new work, and adds qualified jobs to the review
queue. Refresh <http://localhost:8080> after the run completes.

Dagster starts these schedules automatically while `dagster dev` is running:

| Schedule | Frequency |
|---|---|
| Full discovery | Daily at 07:00 UTC |
| Work-queue drain | Every 15 minutes |
| Rejected-job review sample | Daily at 00:15 UTC |
| Langfuse projection | Every minute when Langfuse credentials are set |

Jina and OpenRouter usage can incur charges. Check both providers' usage pages
after the first run.

## Contribute to Job Finder

You can run the unit tests without Jina or OpenRouter credentials. Use Python
3.12 and uv 0.12.9 or newer so `uv.lock` parses consistently with CI.

### Set up the development environment

1. Fork and clone the repository.
2. Install the dependencies and Git hooks:

```sh
uv sync --frozen
uv run pre-commit install --install-hooks -t pre-commit -t commit-msg
```

3. Run the unit tests:

```sh
uv run pytest
```

For database contract tests, complete steps 2 and 3 from the personal setup,
then run:

```sh
uv run pytest contracts
```

The contract tests use `JOB_FINDER_TEST_POSTGRES_DSN`. They create isolated
schemas inside that database.

### Run the checks

Run the same checks as CI before opening a pull request:

```sh
uv run ruff format --check .
uv run ruff check .
uv run basedpyright --level error
uv run vulture
uv run xenon --max-absolute C --max-modules B --max-average A job_finder
uv lock --check
uv run dagster definitions validate -m job_finder.dagster
uv run pytest
uv run pytest contracts
```

Pre-commit also requires [Conventional Commits](https://www.conventionalcommits.org/),
for example `fix: preserve the review decision after refresh`.

### Find your way around

| Path | Responsibility |
|---|---|
| `job_finder/discovery/` | Search catalog, Jina integration, and exchange rates |
| `job_finder/jobs/` | Scraping, structural filters, enrichment, and decisions |
| `job_finder/evaluation/` | Prompts, OpenRouter calls, releases, and evaluation records |
| `job_finder/pipeline/` | Discovery and work-queue orchestration |
| `job_finder/review/` | FastHTML review app and PostgreSQL queries |
| `job_finder/dagster.py` | Assets, jobs, schedules, and resources |
| `job_finder/migrations/` | Ordered PostgreSQL schema migrations |
| `contracts/` | PostgreSQL and Dagster integration tests |
| `docs/architecture-rewrite.md` | Architecture decisions and system boundaries |

Keep pull requests focused, include tests for behavior changes, and explain how
you verified the change.

## Architecture

```text
Dagster (schedules, pools, run history)
  `- job_finder pipeline: discover -> claim -> scrape -> structural filter
     -> evaluate (OpenRouter) -> enrich -> deduplicate -> terminal decision
FastHTML review app  <- reads/writes ->  PostgreSQL (the only authority)
Langfuse             <- retryable projections (never a decision input)
```

PostgreSQL owns every durable fact: jobs, immutable snapshots, pipeline runs,
immutable prompt releases, model-call attempts, review queue items, and
append-only feedback. Dagster owns schedules and run history. Langfuse receives
retryable copies of evaluation traces. A Langfuse outage never changes a job
decision.

## Optional environment variables

The tuning values in `.env.example` work for a local installation. These
variables change optional behavior or production settings:

| Variable | Purpose |
|---|---|
| `ENABLE_ATS_ENRICHMENT` | Enable direct ATS enrichment. Defaults to `true`. |
| `JOB_FINDER_SEARCH_WORKER_COUNT` | Concurrent discovery searches. Defaults to `8`. |
| `JOB_FINDER_WORK_BATCH_SIZE` | Jobs claimed by each queue run. Defaults to `100`. |
| `JOB_FINDER_WORK_LEASE_SECONDS` | Work-item lease duration. Defaults to `3600`. |
| `JOB_FINDER_WORK_RETRY_SECONDS` | Delay before retrying a failed item. Defaults to `300`. |
| `JOB_FINDER_DISCOVERY_HEARTBEAT_URL` | Ping a Better Stack heartbeat after discovery. |
| `JOB_FINDER_WORK_QUEUE_HEARTBEAT_URL` | Ping a Better Stack heartbeat after a queue run. |
| `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` | Enable evaluation-trace projection to Langfuse. |
| `LANGFUSE_BASE_URL` | Langfuse API URL. Defaults to `https://cloud.langfuse.com`. |
| `LANGFUSE_TRACING_ENVIRONMENT` | Langfuse environment name. Defaults to `production`. |
| `JOB_FINDER_REVIEW_COOKIE_SECURE` | Require HTTPS for the review cookie. Use `false` only on localhost. |

## Deployment

One Docker image serves three roles on Railway. Set each service's start
command:

- Review app: `uv run --no-sync uvicorn scripts.serve_review:create_app --factory --host 0.0.0.0 --port $PORT`
- Dagster webserver: `uv run --no-sync dagster-webserver -h 0.0.0.0 -p $PORT -w workspace.yaml`
- Dagster daemon: `uv run --no-sync dagster-daemon run -w workspace.yaml`

`railway.toml` contains the shared build configuration. Configure health checks,
start commands, PostgreSQL, and environment variables on each service. Merges
to `main` create a CalVer tag and a GitHub release with generated notes.

## License

Job Finder is available under the terms in [LICENSE](LICENSE).
