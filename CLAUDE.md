# CLAUDE.md

Guidance for Claude Code (and any other agent) working in this repository.

This is a single-app Bun project. There is no monorepo, no database, no
frontend. The thing this repo does is scrape job boards, run them through an
LLM evaluation pipeline, and write qualified jobs to Notion. Everything else
exists to make that loop reliable, observable, and cheap.

The bar for changes is: the next run is at least as trustworthy as the last
one. False positives in evaluation cost more than false negatives, so we err
toward strictness; flaky LLM behaviour gets contained, not papered over.

## Tasks

```sh
bun run scrape             # full pipeline: search → process → reconcile
bun run reconcile          # reconcile-only (no scrape)
bun run test               # unit tests (src/**/__tests__/)
bun run test:integration   # full eval-pipeline tests, hits real LLM
bun run lint               # biome check
bun run lint:fix           # biome check --write
bun run typecheck          # tsc --noEmit
```

Pre-commit runs `lint`, `typecheck`, and `test` in parallel via lefthook. If a
hook fails, fix the cause — do not bypass with `--no-verify`.

## Runtime: Bun

- Use `bun` for everything: `bun <file>`, `bun test`, `bun install`,
  `bun run <script>`. Never `node`, `npm`, `pnpm`, `ts-node`, `jest`, `vitest`.
- Bun loads `.env` automatically; do not add `dotenv`.
- Prefer Bun built-ins: `Bun.file`, `Bun.$`, `bun:sqlite`, `bun:test`.
- Tests use `import { test, expect } from "bun:test"`.

## Architecture

A linear pipeline. Each stage is a module under `src/pipeline/`; cross-cutting
concerns live alongside.

```
search → scrape → structuralFilter → dedup → enrich → evaluate → reconcile → Notion
```

```
src/
  pipeline/        the stages above, plus processUrl.ts that composes them
    __tests__/     unit tests
    __integration__/  full-pipeline tests + .md fixtures
  services/        external integrations (LLM, Notion, ATS, Slack, exchangeRates, http)
    llm.ts         the only place that talks to OpenAI/OpenRouter
    notion/        client + queries + mutations + builders + helpers
    ats/           dispatcher for greenhouse/lever/ashby
  concurrency/     reusable primitives — Semaphore, RateLimiter, CircuitBreaker, retry
  config/          env loaded + Zod-validated + frozen at startup
  scripts/ (top-level) one-off operational scripts (reevaluate, migrate, etc.)
```

`src/index.ts` is the entrypoint that wires the stages together for the
`scrape` and `reconcile` commands.

## Hard rules

- **Conventional commits.** Enforced by the `commit-msg` hook. Types:
  `feat|fix|refactor|chore|docs|test|style|perf|ci|build|revert`. The squash
  commit on merge becomes a line in the auto-generated release notes — write it
  like a release note, not a diary entry.
- **Atomic commits.** One logical change per commit. Don't fold unrelated
  cleanup into a feature commit.
- **Never bypass hooks.** No `--no-verify`. If lefthook fails, the underlying
  problem is the bug, not the hook.
- **Env vars only via `src/config`.** All `process.env` access lives in
  `config/schema.ts`, validated by Zod, frozen at startup. The app refuses to
  start with bad config — that is the point.
- **Structured logging via Pino child loggers.** Pass structured fields
  (`log.info({ url, attempt }, "search retry")`), not interpolated strings.
  Never log secrets.
- **Fail loud.** Errors propagate or are explicitly handled. Silent `catch`
  blocks are forbidden. Green CI with broken runtime is worse than red CI.
- **Tests must always run.** No `skipIf`, no `describe.skip`, no conditional
  skipping. If a test needs an env var, it should fail loudly when missing —
  not silently pass.
- **Fixed-version dependencies.** Every entry in `package.json` is pinned to an
  exact version. No `^`, no `~`. Updates are deliberate, lockfile-checked, and
  land in their own commit.
- **Modules are domain models.** A file's name describes the subject it
  owns (`exchangeRates.ts`, `notionCache.ts`, `tokenTracker.ts`). If a
  candidate filename describes a role rather than a piece of the domain,
  push back on the design — you probably haven't decided what it is yet.

## Type system

Lean on compile-time checks. Anything `tsc`, Biome, or lefthook can catch
before the code runs is the cheapest place to catch it. Save runtime checks
for what types can't see — config from the environment, network responses,
LLM output. A linter rule that fires on every save beats a unit test that
fires once an hour.

- **Parse at boundaries.** Validate config, LLM tool-call responses, and
  ATS payloads with Zod the moment they enter the system. Don't
  `JSON.parse` and cast — let the schema fail loudly so a malformed input
  never reaches the rest of the code.
- **Discriminated unions over boolean flags.** Model state with a `status`
  or `kind` field. Combinations of `isReady` / `isComplete` invite invalid
  states; a tagged union doesn't.
- **`as const` for shared constants and error codes** so callers get a
  narrow type and the values inline at compile time.
- **`noExplicitAny` and `noNonNullAssertion` are Biome errors.** If you
  reach for `as any` or `!`, the type model is wrong — fix the model.

### Direction: `neverthrow` for new fallible domain code

We are gradually adopting `Result<T, E>` / `ResultAsync<T, E>` for fallible
domain functions. The rule:

- New fallible domain code returns a `Result` and uses typed `as const` error
  codes — not strings, not exceptions.
- Existing code that throws stays as-is until we touch it. When you touch it,
  prefer converting it.
- Boundary helpers and entrypoints (`src/index.ts`, scripts) are allowed to
  catch + log + exit. They are the place errors finally surface.

This is a direction, not a retrofit. Don't open a PR that rewrites the world.

## Testing

Two layers. Unit tests in `src/**/__tests__/` guard the deterministic
parts — dedup, structural filter, scrape parsing, the concurrency
primitives — and run on every commit via lefthook. Integration tests in
`src/**/__integration__/` guard the LLM-driven verdicts via fixture
batches; they hit OpenRouter and cost money, so run them with
`bun run test:integration` before merging anything that changes prompts
or the eval pipeline.

- **Test names are third-person verbs.** `test("returns the canonical url")`,
  `describe("Dedup")`. Names describe behaviour, not implementation.
- **All tests always run.** Never `skipIf`, never `describe.skip`. A test that
  needs `OPENROUTER_API_KEY` should fail loudly without it.
- **Integration tests run the full eval pipeline.** `evaluateJob` (filters
  AND-ed, profiles OR-ed) — not isolated criteria. The contract under test is
  the system's verdict, not any single LLM call.
- **Fixtures are body-visible Markdown files.** Every fixture in
  `src/pipeline/__integration__/fixtures/` is a `.md` document whose reject
  reason is *visible in the body text*. Don't fixturise jobs whose reject
  reason only exists in ATS API metadata — those tests can't be reproduced
  from the fixture alone.
- **Parallel LLM calls in `beforeAll` with `Semaphore` + per-task error
  recovery.** Run all evaluations once, in parallel, with a bounded
  `Semaphore`. Wrap each task so a single failure doesn't tank the suite —
  store the error on the result and assert on it in its own test.
- **Track FP and FN separately.** False positives (a bad job marked pass) cost
  more than false negatives (a good job marked reject). The integration suite
  asserts a stricter FP threshold than FN.
- **N-shot examples in filter prompts.** Small, fast models (Gemini Flash,
  Haiku) need worked examples to be reliable. New filter prompts ship with
  N-shot examples *and* fixtures that exercise them.

## LLMs are non-deterministic black boxes

Treat every LLM call like a coin flip with strong opinions. The model
can be unreliable, will occasionally hallucinate, and sometimes returns
fluent nonsense. The pipeline keeps working anyway because composition
— structural filter, AND-ed filters, OR-ed profiles, fixture-pinned
thresholds — enforces decisions; the LLM only informs them.

- **All LLM calls go through `services/llm.ts`.** It owns the OpenRouter
  client. No direct `openai` imports anywhere else.
- **Token usage is tracked.** Every call logs token usage via `TokenTracker`.
  If you add a new LLM call site, wire the tracker through.
- **LLM bursts are bounded.** Stack `Semaphore` → `CircuitBreaker` →
  `withRetry` from `src/concurrency/`. Don't reinvent.
- **Failure modes are defined.** Timeout, malformed `tool_call`, rate limit —
  each is handled explicitly. Malformed `tool_call` throws so the retry stack
  catches it; do not silently return a default verdict.
- **Correctness must not depend on a specific LLM output.** Fixtures, profile
  rules, and the AND/OR composition enforce decisions; the LLM informs them.
  A single LLM hallucination should change at most one job's outcome, never the
  pipeline's behaviour.
- **Prompt changes ship with fixtures.** New prompt behaviour (added
  criterion, changed N-shot example) gets a fixture that exercises it — both a
  passing case and a rejecting case.

## Notion as database

Notion is the system of record. Treat it accordingly.

- **All Notion access goes through `services/notion`.** Builders compose
  page properties, mutations write, queries read, helpers normalise. No raw
  `@notionhq/client` calls outside this directory.
- **One cache per run.** `notionCache.ts` pre-loads what we need; mutations
  flow through `NotionCacheUpdater` so the in-memory view stays consistent.
- **Reconcile is idempotent.** A second `reconcile()` call must not change
  steady state. Both pre-scrape and post-scrape passes run every full run.
- **Notion is rate-limited and eventually consistent.** Expect 429s. Retries
  go through `withRetry`. Don't assume read-after-write within a run.
- **Dedup canonicalises URLs.** See `pipeline/dedup.ts`. New job sources need
  their URL shape considered there before they're trusted as unique.

## Concurrency primitives

`src/concurrency/` exports composable primitives. Use them; do not reinvent.

- `Semaphore` — bounded parallelism (e.g. `jinaSearchSemaphore`).
- `RateLimiter` — RPS cap.
- `CircuitBreaker` — fail-fast on persistent upstream failure.
- `withRetry` — jittered backoff with `shouldRetry` and `onRetry` hooks.

Upstream calls are stacked: `Semaphore.run(() => Breaker.run(() => withRetry(...)))`.
The order matters — the semaphore bounds total in-flight work, the breaker
fails fast on a sick upstream, retries handle transient flakes inside that.

## Git workflow

- **Conventional commits**, atomic, per logical change.
- **Branches** are flat: `feat-ats-dispatcher`, not `feature/ats-dispatcher`.
- **Squash merge to `main` triggers a release.** `.github/workflows/release.yml`
  cuts a CalVer tag and a GitHub Release with auto-generated notes from commit
  messages. The squash commit title shows up verbatim in those notes — write
  it for the reader.
- **Solo dev workflow.** No required reviews. Self-review with `/review`
  before merge.
- `Co-Authored-By` trailers are fine.

## Style

- Biome handles formatting and lint. Don't fight it; run `bun run lint:fix`.
- Imports are relative within `src/`. No path aliases configured.
- Keep functions short enough to read without scrolling.
