# job-finder

Job Finder searches job boards, evaluates each listing against your criteria,
and puts the strongest matches in a private review queue. It uses Dagster to
run the search pipeline, PostgreSQL to store results, Jina to find and scrape
listings, and OpenRouter to evaluate them.

[![I made my job search even easier with AI (I improved my job finder)](https://i.ytimg.com/vi/bO7vzA0xbWg/hqdefault.jpg)](https://youtu.be/bO7vzA0xbWg)

[Watch the Job Finder demo on YouTube](https://youtu.be/bO7vzA0xbWg).

This README has four setup paths:

- [Deploy on Railway for yourself](#deploy-on-railway-for-yourself) if you want
  Railway to create the connected services for you.
- [Deploy a hosted instance for an owner](#deploy-a-hosted-instance-for-an-owner)
  if you are onboarding someone yourself.
- [Run Job Finder for yourself](#run-job-finder-for-yourself) if you want job
  results as soon as possible.
- [Contribute to Job Finder](#contribute-to-job-finder) if you want a local
  development environment.

## Deploy on Railway for yourself

[Deploy Job Finder on Railway](https://railway.com/deploy/job-finder). The
template creates the review app, PostgreSQL, and two private Dagster services.
Wait for all four services to become healthy, then open the public domain on
`review`. The first visit redirects to `/setup`.

In Railway, open the `review` service's Variables tab and copy the generated
`JOB_FINDER_BOOTSTRAP_TOKEN`. Use it on `/setup` to create your owner account.
Enter your Jina and OpenRouter API keys, job preferences, and spend budget in
the app, then run the bounded test search. Remove the bootstrap token from the
Railway service after the owner account exists. See the
[template setup guide](docs/railway-template.md) for the service layout and
first-run check.

## Deploy a hosted instance for an owner

Follow the [hosted owner onboarding runbook](docs/hosted-owner-onboarding.md)
for the exact sequence to provision an instance, run the setup call, test the
search, and enable daily discovery.

The operator command creates one Railway project per owner. It provisions
managed PostgreSQL, a private Dagster webserver and daemon, and a public review
app. It generates the session, credential-encryption, and first-owner secrets
and enables the independent search setup flow. During the setup session, enter
the provider keys yourself, then work through search preferences and the spend
budget with the owner. No SQL, environment-file editing, or Dagster access is
needed during onboarding.

Install Python 3.12 and the [Railway CLI](https://docs.railway.com/cli), then
sign in with `railway login`. Run `railway whoami --json` to find the ID of the
workspace that will pay for and operate this owner's instance. Run from a clone
of this repository:

```sh
python3 scripts/deploy_railway.py --name job-finder-owner-name --workspace YOUR_WORKSPACE_ID
```

Use a unique project name for each owner. The command prints the project URL,
waits for the three application services to deploy, then prints the review URL
and a one-time bootstrap token. Keep the token private and enter it with the
owner on the first visit to create their account. Enter your provider keys in
the app, not in Railway. Save and publish the acquisition policy and
qualification definition separately, activate acquisition, set the budget,
then run the bounded test search. The test search uses a candidate qualification
target without activating it for scheduled runs. After reviewing its results,
an operator must [prepare canonical qualification evidence](docs/qualification-evidence-operator.md)
for every phase. Use the qualification promotion tools to approve and activate
that target only after the evidence preview passes. Scheduled
discovery waits for this activation. The app's `/readyz` endpoint is the Railway
health check; only the review service receives a public domain.

If a deployment fails before any services are created, resume that exact empty
project with `--project PROJECT_ID`. If services already exist, use the printed
project URL to inspect their logs and variables. The command never deletes a
project automatically; running it again without `--project` creates a separate
project with new secrets and data.
Once the owner is set up, remove `JOB_FINDER_BOOTSTRAP_TOKEN` from the review
service variables in Railway.

### Hosted deployment smoke check

For a fresh instance, check the following before handing it to an owner:

1. The Railway project has four services: Postgres, review, dagster-webserver,
   and dagster-daemon. Only review has a public domain; Postgres has no public
   TCP proxy.
2. The review URL's `/readyz` returns HTTP 200, and the landing page redirects
   to owner setup.
3. With the generated bootstrap token, create the owner account, enter provider
   credentials and preferences, set a spend budget, and run the bounded test
   search. Confirm progress and results appear in the browser.
4. Check `/operations/control` after setup. It should show the expected schedules
   without exposing Dagster's GraphQL service publicly.

## Run Job Finder for yourself

The steps below run the full application with Docker Compose. The default search
and evaluation criteria target senior product engineering and applied AI roles
that can be worked remotely from Europe. Use the **Search setup** page to change
those defaults before your first search.

### 1. Install the requirements

Install:

- [Git](https://git-scm.com/downloads)
- [Docker](https://docs.docker.com/get-started/get-docker/)
- A [Jina API key](https://jina.ai/api-dashboard/)
- An [OpenRouter API key](https://openrouter.ai/settings/keys) with enough
  credit for model calls
- A Typesafe API key for the default relevance policy

Clone the repository:

```sh
git clone https://github.com/mauricedesaxe/job-finder.git
cd job-finder
```

### 2. Configure the application

Create your local environment file:

```sh
cp .env.example .env
```

Open `.env` and replace these values:

- `JINA_API_KEY`
- `OPENROUTER_API_KEY`
- `TYPESAFE_API_KEY`
- `JOB_FINDER_REVIEW_SESSION_SECRET`, with at least 32 random characters
- `JOB_FINDER_BOOTSTRAP_TOKEN`, with at least 32 random characters for a fresh deployment
- `JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY`, generated with the command below

Generate each secret separately with:

```sh
docker run --rm python:3.12-alpine \
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Compose supplies private PostgreSQL and Dagster URLs, so the loopback URLs in
`.env` are only used for native development. Set `POSTGRES_PASSWORD` in `.env`
to replace the local database password; use a URL-safe value. If port 5432 is
already in use, change `POSTGRES_PORT`. PostgreSQL binds only to localhost so
native development and contract tests can reach it.

### 3. Start Job Finder

Build and start the complete stack:

```sh
docker compose up --build --detach --wait
```

Open the private review app at <http://localhost:8080>. The Dagster webserver and
daemon remain on the internal Compose network. PostgreSQL is available only on
the configured localhost port. The database is stored in the `postgres-data`
volume and survives restarts.

On the first visit, enter `JOB_FINDER_BOOTSTRAP_TOKEN` and create the owner
password. The token proves the first visitor controls the deployment; Job
Finder hashes the password into PostgreSQL before continuing. Upgrading
installations can leave their existing `JOB_FINDER_REVIEW_PASSWORD` set for one
startup; Job Finder imports it once, after which the environment value can be
removed.
The authenticated home reads and controls the four Job Finder schedules through
Dagster's GraphQL API. In deployment, set `JOB_FINDER_DAGSTER_GRAPHQL_URL`
to the Dagster webserver's internal `/graphql` URL.

Use `docker compose logs --follow` to inspect startup or pipeline logs. Stop the
stack with `docker compose down`. To also permanently delete all local Job
Finder data, run `docker compose down --volumes`.

### 4. Set your job criteria

Open **Search setup** and review the search keywords, enabled job boards,
personal criteria, and target profiles. Use the separate actions in order:

1. Select **Preview unsaved values** to validate the form and inspect the
   generated searches and prompts.
2. Select **Save draft** to store the values without changing a running search.
3. Select **Publish saved draft** to create an immutable configuration revision
   and prompt release.
4. Select **Activate published draft** to use the revision for new searches.

Search configuration activation changes discovery for new runs. A criteria or
profile change also creates a prompt-release candidate. Evaluate and approve
that candidate through the MCP release-target workflow before activating it for
production evaluation. Existing runs keep the configuration and release target
that they captured when they started.

### Connect an MCP client

Run `scripts/serve_mcp.py` as a stdio MCP server. Start the command from the
repository root with `JOB_FINDER_POSTGRES_DSN` in its environment:

```sh
uv run python scripts/serve_mcp.py
```

Configure your MCP client to start that command instead of starting it in a
terminal. The server exposes bounded tools for feedback curation, evaluation
manifests, search configuration validation, preview, publication, history and
activation, and Langfuse projection status. It applies pending database
migrations before accepting requests.

For configuration changes, call the tools in this order:

1. Read `configuration_active_get` and `configuration_draft_get`.
2. Check new values with `configuration_validate` and `configuration_preview`.
3. Save with `configuration_draft_update` and the observed draft version.
4. Publish with `configuration_publish`, a stable idempotency key, and the
   expected draft version and configuration revision ID.
5. Read active state again, then call `configuration_activate` with the observed
   revision and generation.

For criteria or profile changes, promote the published prompt release:

1. Read the baseline target and generation with `release_target_active_get`.
2. Build the candidate with `release_target_candidate_create`. Use the prompt
   release ID from `configuration_preview` or the published revision.
3. Select one frozen manifest with `manifest_list` or create one with
   `manifest_create`.
4. Call `evaluation_run` twice against that manifest: once for the active
   baseline target and once for the candidate target.
5. Pass both completed run IDs to `release_target_compare`.
6. Record the result with `release_target_decide`.
7. Call `release_target_activate` with the approval ID and the baseline target
   and generation from step 1.

Activation requires approval for the exact prompt and relevance release pair.

When a deployment changes a relevance execution file, its source hash changes and
the old target cannot execute under the new code. Pause discovery and finish a
baseline evaluation on a frozen manifest before deploying. After deploying,
create the new target, evaluate it on the same manifest, compare the stored
baseline and candidate runs, approve the result, and activate the new target
before resuming discovery. Keep the old release and run records unchanged. The
baseline cannot be rerun under the new code because its source hash no longer
matches.

#### Roll back a configuration

Configuration revisions and publications are immutable. To roll back, use
`configuration_revision_list` and `configuration_revision_get` to select an
older published revision. Read `configuration_active_get`, then pass the current
revision and generation to `configuration_activate`. The rollback applies only
to new runs.

Prompt and relevance releases have a separate active pointer. To restore an old
release target, treat it as the candidate in the same workflow: read the current
baseline, run both targets against one manifest, compare them, record a new
approval, and activate the approved old target.

### 5. Run your first search

Open the authenticated home, find **Full discovery**, and select **Run now**.
The run discovers listings, evaluates new work, and adds qualified jobs to the
review queue. It pins the active configuration and release target when it
starts. Refresh <http://localhost:8080> after the run completes.

The private Dagster daemon starts these schedules automatically:

| Schedule | Frequency |
|---|---|
| Full discovery | Daily at 07:00 UTC |
| Work-queue drain | Every 15 minutes |
| Rejected-job review sample | Daily at 00:15 UTC |
| Langfuse projection | Every minute when Langfuse credentials are set |

Jina, OpenRouter, and Typesafe usage can incur charges. Check the relevant
providers' usage pages after the first run.

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
| `job_finder/mcp_server.py` | FastMCP tools for feedback, evaluation, and search configuration workflows |
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
immutable configuration revisions, active configuration and release pointers,
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

The hosted deployment command above configures the three roles of the Docker
image and their private PostgreSQL and Dagster connections. `railway.toml` is
retained for existing Railway services; new projects use the Dockerfile and the
settings set by the command. Merges to `main` create a CalVer tag and a GitHub
release with generated notes.

## License

Job Finder is available under the terms in [LICENSE](LICENSE).
