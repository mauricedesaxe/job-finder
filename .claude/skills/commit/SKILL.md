---
name: commit
description: Commit the changes made in this session as one or more atomic conventional-commit-formatted commits, immediately. Use when a logical chunk of work is finished and ready to be recorded — preferably as you go, not all at once at the end. Use when Alex says "commit", "/commit", "commit what we did", or whenever you've completed a discrete piece of work and want it on the branch before moving on.
---

# Commit Skill

Commit the work from this session, now. The bias is toward landing
small, atomic commits as soon as a logical chunk is finished — not
batching a session's worth of edits into one megacommit at the end.

Two non-negotiables in this repo:

- **Conventional commits.** The `commit-msg` hook enforces
  `^(feat|fix|refactor|chore|docs|test|style|perf|ci|build|revert)(\(.+\))?: .+`.
- **No `--no-verify`.** The `pre-commit` hook runs `bun run lint`,
  `bun run typecheck`, and `bun run test` in parallel. If something
  fails, fix the cause and try again. Don't bypass.

## Commit as you go

Don't accumulate a session's worth of edits into one commit:

- Atomic commits are cheaper to revert and easier to read in `git
  blame` / `git log`.
- The squash commit on merge becomes a release-note line, but the
  individual commits on the branch still serve any future archaeology.
- A failing pre-commit hook on a 200-line tangle is much harder to
  debug than on a 30-line focused change.

When you finish a discrete change — a feature, a fix, a refactor, a
doc edit — invoke this skill, commit, and continue. Don't wait for
"approval" before each commit. The atomic conventional-commit format
*is* the discipline; running it is the action.

## Step 1: check what's already staged

```sh
git diff --cached --name-only
```

If anything is listed, those files were staged from before this
conversation started. They are not ours to commit. Either unstage them
(`git reset <file>`) or stop and ask what to do — sweeping them into
this commit would mix unrelated work into our change.

## Step 2: identify what changed in this session

Walk the conversation: which files did you `Edit`, `Write`, or create?
Cross-reference against the working tree:

```sh
git status --short
git diff --stat
```

Only commit files that (a) we touched in this session AND (b) have
uncommitted changes right now. Other modified files are pre-existing
work that belongs to a different commit.

## Step 3: group into atomic units

One commit is one logical change. The bar: would `git revert <sha>`
of this commit alone leave the codebase in a sane state?

- A new pipeline stage + its unit tests + the index export → one
  commit (`feat: add <stage>`).
- A prompt change + the fixtures that exercise it → one commit
  (`feat: tighten <criterion> filter`).
- A bug fix in module A + a refactor in module B → two commits.

If the session's work is one coherent thing, one commit. If it
sprawled across separate concerns, split before committing.

## Step 4: pick the conventional-commit type

| Type       | When                                                      |
| ---------- | --------------------------------------------------------- |
| `feat`     | new functionality (new pipeline stage, new prompt)        |
| `fix`      | bug fix in existing functionality                         |
| `refactor` | restructuring without behaviour change                    |
| `chore`    | tooling, configs, agents/skills, lockfile bumps           |
| `docs`     | docs-only changes (`README.md`, `CLAUDE.md`, comments)    |
| `test`     | test-only changes                                         |
| `style`    | formatting only (rare — Biome handles it)                 |
| `perf`     | performance work without behaviour change                 |
| `ci`       | `.github/workflows/`, `lefthook.yml`                      |
| `build`    | `package.json`, `tsconfig.json`, `bun.lock`               |
| `revert`   | reverting a previous commit                               |

A scope (`feat(ats):`) is optional — only worth it when the type alone
is ambiguous.

## Step 5: stage and commit

Stage files explicitly — never `git add -A` or `git add .`, which risks
pulling in pre-staged or unrelated work. Then commit using multiple
`-m` flags so the subject, body, and any trailers are joined with
blank lines without HEREDOC ceremony:

```sh
git add src/pipeline/dedup.ts src/pipeline/__tests__/dedup.test.ts
git commit \
  -m "feat: short-circuit dedup on exact case-insensitive match" \
  -m "Avoids one LLM call per page when the title already exists in the cache verbatim. The fuzzy LLM check still runs for non-exact matches." \
  -m "Co-Authored-By: <your current Claude model> <noreply@anthropic.com>"
```

Use whatever Claude model is actually running this session in the
trailer — don't hard-code a specific model name, since it goes stale on
the next bump.

Each `-m` becomes its own paragraph in the message. Subject is the
first one; the rest become body and trailers in order.

## When the hook fails

If the pre-commit hook fails, **the commit did not happen**. Read the
output, fix the underlying problem (lint error, type error, failing
test), re-stage, and create a NEW commit attempt. Don't reach for
`--amend` — there is no commit to amend.

## Style for messages

- Subject: imperative mood, lowercase first word, no trailing period,
  ~50 characters. Examples that pass and fit the existing log:
  - `feat: add ATS dispatcher and markdown formatter`
  - `fix: accept null in optional ATS schema fields`
  - `refactor: extract NotionCacheUpdater from notionCache`
- Body (optional, second `-m`): explain *why*, not *what*. Wrap at
  ~72 characters per line. Skip the body when the subject is enough.
- A `Co-Authored-By: <current Claude model> <noreply@anthropic.com>`
  trailer is fine — fill in whichever model is actually running.

If `git status` is clean (nothing from this session survived), say so
and stop. Never create empty commits.
