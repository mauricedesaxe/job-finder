# CLAUDE.md

Guidance for Claude Code (and any other agent) working in this repository.

This is a single-app Python project deployed to Railway from one Docker image.
Dagster starts work, FastHTML renders the daily review, PostgreSQL owns every
durable fact, and Langfuse receives retryable projections.
`docs/architecture-rewrite.md` records the rewrite that produced this shape;
the legacy Bun/D1/Cloudflare runtime is deleted.

The bar for changes is: the next run is at least as trustworthy as the last
one. False positives in evaluation cost more than false negatives, so we err
toward strictness; flaky LLM behaviour gets contained, not papered over.

## Tasks

```sh
uv sync --frozen           # install locked dependencies
uv run pytest              # unit tests (job_finder/**/test_*.py)
uv run pytest contracts    # PostgreSQL authority + Dagster contracts (needs a real Postgres)
uv run dagster definitions validate -m job_finder.dagster
uv run ruff format --check . && uv run ruff check .
uv run basedpyright --level error
uv run python -m scripts.evaluate_corpus   # full eval pipeline against real OpenRouter
```

Pre-commit runs ruff format, ruff check, basedpyright, and unit tests on every
commit, and a commit-msg hook enforces conventional commits. Install with
`uv run pre-commit install --install-hooks -t pre-commit -t commit-msg`. If a
hook fails, fix the cause — do not bypass with `--no-verify` or `LEFTHOOK`-style
escapes.

## Runtime: uv

- Use `uv` for everything: `uv run <tool>`, `uv sync`, `uv lock`. Never `pip
  install` into the system, never add a second package manager.
- Dependencies are pinned to exact versions in `pyproject.toml` and locked in
  `uv.lock`. Updates are deliberate, lockfile-checked, and land in their own
  commit (`uv lock --check` enforces the lock in CI).
- Tests use `pytest`. Contracts that need PostgreSQL read
  `JOB_FINDER_TEST_POSTGRES_DSN` and fail loudly when it is missing.

## Architecture

```
Dagster (schedules, pools, run history)
  └─ job_finder pipeline: discover → claim → scrape → structural filter
     → evaluate (OpenRouter) → enrich → dedup → terminal decision
FastHTML review app  ← reads/writes →  PostgreSQL (the only authority)
Langfuse             ← retryable projections (never a decision input)
```

- `job_finder/` is the application package, organized by domain:
  - `discovery/` — Jina search, query catalog, exchange rates.
  - `jobs/` — scraping, structural filter, enrichment, dedup, decision pipeline.
  - `evaluation/` — prompts, prompt releases, OpenRouter execution, manifests,
    Langfuse projection.
  - `pipeline/` — orchestration runs, claiming, leases.
  - `review/` — FastHTML app, frozen daily review membership, feedback events.
  - `ats/` — Greenhouse/Lever/Ashby/Workable adapters.
  - `migrations/` — ordered SQL migrations applied by `job_finder.database`.
- `contracts/` — tests that pin the PostgreSQL authority contract and Dagster
  orchestration against a real database.
- `scripts/` — operational entry points (`serve_review.py`, `evaluate_corpus.py`,
  `bootstrap_postgres_prompts.py`).
- `src/index.ts` is gone; `job_finder/dagster.py` composes the Dagster
  definitions and `scripts/serve_review.py` is the review app's ASGI factory.

## Hard rules

- **Conventional commits.** Enforced by the commit-msg hook. Types:
  `feat|fix|refactor|chore|docs|test|style|perf|ci|build|revert`. Pull request
  titles feed the generated release notes. Keep commit subjects ready for
  permanent `main` history.
- **Atomic commits.** One logical change per commit. Don't fold unrelated
  cleanup into a feature commit.
- **Never bypass hooks.** No `--no-verify`. If pre-commit fails, the underlying
  problem is the bug, not the hook.
- **Configuration only through `job_finder/config.py`.** Pydantic settings
  classes read environment variables, validate them, and freeze at startup.
  Production `os.environ` access lives there and nowhere else. Scripts may read
  their own task-specific flags, and tests may construct settings fixtures.
- **Fail loud.** Errors propagate or are explicitly handled. Silent `except`
  blocks are forbidden. Green CI with broken runtime is worse than red CI.
- **Tests must always run.** No `skipif`, no conditional skipping. If a test
  needs an env var or a database, it should fail loudly when missing — not
  silently pass.
- **Fixed-version dependencies.** Every entry in `pyproject.toml` is pinned to
  an exact version. No `>=` ranges for runtime deps. Updates are deliberate,
  lockfile-checked, and land in their own commit.
- **Modules are domain models.** A file's name describes the subject it owns
  (`prompt_releases.py`, `title_deduplication.py`, `frozen daily reviews` live
  in `review/postgres.py`). If a candidate filename describes a role rather
  than a piece of the domain, push back on the design.

## Type system

Lean on compile-time checks. Anything ruff, basedpyright, or the hooks can
catch before the code runs is the cheapest place to catch it. Save runtime
checks for what types can't see — config from the environment, network
responses, LLM output.

- **Parse at boundaries.** Validate config, OpenRouter tool-call responses, ATS
  payloads, and scrape output with Pydantic the moment they enter the system.
  Don't `json.loads` and cast — let the model fail loudly so a malformed input
  never reaches the rest of the code.
- **Discriminated unions over boolean flags.** Model state with tagged unions
  (`Rejected`, `PromptAccepted`, `OperationalError`) so invalid states are
  unrepresentable.
- **No silent `Any`.** basedpyright runs at error level. If you reach for a cast
  or an ignore, fix the type model instead.
- **Frozen models for shared state.** Settings and domain facts are immutable;
  explicit state transitions beat in-place mutation.

## Testing

Three layers. Unit tests beside the modules (`job_finder/**/test_*.py`) cover
the deterministic parts and run on every commit via pre-commit. Contracts in
`contracts/` pin the PostgreSQL authority (immutability, retry identities,
transaction rollback) and Dagster orchestration against a real Postgres; CI
runs them on every push. The corpus evaluation
(`uv run python -m scripts.evaluate_corpus`) runs the full eval pipeline —
structural filter AND-ed with filter criteria, profiles OR-ed — against real
OpenRouter before merging anything that changes prompts.

- **Test names are third-person verbs.** `test("returns the canonical url")`.
- **All tests always run.** Never skip. A test that needs
  `JOB_FINDER_TEST_POSTGRES_DSN` should fail loudly without it.
- **Fixtures are recorded, not invented.** ATS fixtures in `fixtures/ats/` and
  the evaluation corpus are captured upstream responses. Don't hand-write a
  payload to match what you think the upstream returns.
- **Track FP and FN separately.** False positives (a bad job marked pass) cost
  more than false negatives. Thresholds: FP ≤ 15%, FN ≤ 10%
  (`docs/architecture-rewrite.md`).

## LLMs are non-deterministic black boxes

Treat every LLM call like a coin flip with strong opinions. The pipeline keeps
working anyway because composition — structural filter, AND-ed filters, OR-ed
profiles, frozen fixtures — enforces decisions; the LLM only informs them.

- **Every model attempt is recorded in PostgreSQL** (`model_call_attempts`):
  prompt version, tokens, cost, latency, parsed result. PostgreSQL, not
  Langfuse, is the source of truth.
- **Langfuse is a retryable projection.** `langfuse_projection_items` drains in
  its own Dagster pool. A Langfuse outage must never change a job decision or
  fail job processing. Do not add a code path that reads from Langfuse.
- **Bounded concurrency everywhere.** Use the executor pools, leases, and
  claim semantics in `job_finder/pipeline/` and `dagster.yaml`; don't invent
  ad-hoc threading.
- **Failure modes are defined.** Timeout, malformed `tool_call`, rate limit —
  each maps to retry or terminal failure in `job_finder/evaluation/models.py`.
  A malformed tool_call never becomes a default verdict.
- **Prompt releases are immutable.** Bootstrap prompts are versioned in
  PostgreSQL; promotion checks false positives and false negatives against the
  active release before switching.

## PostgreSQL is the only authority

- **All access goes through `job_finder.database` and the domain modules.**
  Migrations are ordered, immutable once applied (sha256-tracked), and run
  automatically on pipeline connections.
- **Idempotency by identity.** Every retryable write carries an idempotency
  key or exact input identity. Repeating a run creates no duplicate job,
  snapshot, decision, feedback event, or projection.
- **Terminal results commit atomically** with their pending projections; the
  projection drain removes them after confirmed delivery.
- **Daily review membership is frozen** (`review_days`): the day's items are
  chosen exactly once; later-arriving decisions wait for the next day.
- **Feedback is append-only** and points at the exact decision and snapshot
  shown in FastHTML. Raw feedback does not become an evaluation answer without
  curation.

## Dagster

- Dagster owns schedules, pools, sensors, and operational run history — never
  job state or evaluation provenance. Assets call domain operations; domain
  logic stays in `job_finder/`.
- Dagster storage lives in the `dagster` PostgreSQL schema (see `dagster.yaml`
  and `JOB_FINDER_DAGSTER_POSTGRES_DSN`); domain tables stay in `public`. The
  two namespaces must not mix — both name a `jobs` table.
- Schedules: full discovery Wednesdays 07:00 UTC, work-queue drain every 15
  minutes, Langfuse projection every minute (self-enables when Langfuse keys
  are present).

## Deployment

One Docker image (`Dockerfile`) serves three roles on Railway; the per-service
start command decides the role (review app, Dagster webserver, Dagster
daemon). `railway.toml` carries build config only. Required secrets are listed
in `README.md`. Migrations apply automatically on first connection; running
them early from a workstation with the public DSN is fine.

## Version control workflow

- **Jujutsu drives local version control** where available (colocated jj with
  bookmarks named `<type>/<#>-<slug>`); plain git is fine for a solo actor.
- **Conventional commits**, atomic, per logical change.
- **Rebase merge to `main` triggers a release.** `.github/workflows/release.yml`
  cuts a CalVer tag and a GitHub Release with generated notes from the landed
  pull requests. Keep both PR titles and commit subjects release-ready.
- **Verify every release after merge.** Wait for the latest `release.yml` run
  on `main`. Report a failed workflow. On success, report the CalVer tag and
  GitHub Release URL.
- **Solo dev workflow.** No required reviews. Self-review before merge.
- Human `Co-Authored-By` trailers are fine. Do not add AI attribution trailers.

## Style

- ruff handles formatting and lint. Don't fight it; run `uv run ruff format .`.
- Keep functions short enough to read without scrolling. Xenon enforces
  block complexity at C, module at B, average A.
- Imports are absolute within the package (`from job_finder.evaluation import ...`).


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
