---
name: ship
description: Land work on `main` end-to-end. Detects current state (branch, tree, remote, PR) and runs only the steps that are still missing — create a feature branch if Alex is on `main` with changes, commit dirty tree atomically per the commit skill, push, open a PR, wait for CI, preview the squash commit (which becomes the release-note line because every merge triggers `release.yml`), confirm, squash-merge, then surface the CalVer tag and GitHub Release URL once the release workflow completes. Use when Alex says "ship", "/ship", "ship this", "ship it", "merge", "/merge", "land this", "merge this PR", or otherwise wants the work he just did landed on main without thinking about which intermediate step is missing.
---

# Ship Skill

End-to-end "land this work on `main`". Alex may be anywhere in the
flow — fresh changes on `main`, partially committed on a branch,
pushed-no-PR, PR-no-merge — and `/ship` figures out where he is and
runs only the missing steps. It can: create a branch off `main`,
commit a dirty tree (atomic, conventional, per the `/commit` skill),
push, open a PR, gate on CI, preview the release-note-aware squash
merge, merge, and surface the resulting tag.

**Every merge to `main` triggers `.github/workflows/release.yml`**, which
cuts a CalVer tag and a GitHub Release whose notes come from the squash
commit title. Treat the squash title like a release note, because that
is what it becomes.

## Auth quirk specific to this repo

Alex has two `gh` logins: an active `GITHUB_TOKEN` env var with only
`read:packages` scope (good enough for read-only `gh pr view` /
`gh pr checks`), and a keyring login with full `repo`/`read:org` scope.
Mutations need the keyring token — override `GITHUB_TOKEN` per-command
to force gh to fall back to it:

```sh
GITHUB_TOKEN= gh pr merge ...
GITHUB_TOKEN= gh pr create ...
```

`gh pr edit` additionally hits a deprecated GraphQL field
(`repository.pullRequest.projectCards`, Projects-classic sunset) and
fails even with the keyring token. When you need to mutate PR fields
(title, body, labels), use the REST API instead:

```sh
GITHUB_TOKEN= gh api -X PATCH /repos/<owner>/<repo>/pulls/<n> \
  -f title="<new title>" --jq '.title'
```

Read calls (`gh pr view`, `gh pr checks`, `gh run list`,
`gh release list`) work fine on either token — no override needed.

## Step 1: detect current state

`<arg>` may be empty, a PR number (e.g. `42`), or a PR URL.

**If a PR ref was passed**, use it directly and skip to Step 6 (the
PR already exists; no branching, committing, pushing, or PR creation
needed):

```sh
gh pr view <ref> --json number,title,headRefName,baseRefName,state,mergeable,mergeStateStatus
```

If the state is not `OPEN`, stop and explain (already merged, closed,
draft). Do not auto-create another PR — Alex is pointing you at a
specific one.

**If no ref was passed**, gather the local picture in one go:

```sh
git branch --show-current                                              # current branch
git status --short                                                     # working-tree state
git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>/dev/null # upstream tracking
git log --oneline @{u}..HEAD 2>/dev/null                               # unpushed commits
gh pr view --json number,title,headRefName,baseRefName,state,mergeable,mergeStateStatus 2>/dev/null
```

From this, decide which of the following steps actually need to run.
Each step is conditional — skip the ones that are already done.

| If…                                              | Run step(s)            |
| ------------------------------------------------ | ---------------------- |
| On `main` and tree is clean                      | Stop — nothing to ship |
| On `main` and tree has changes                   | Step 2 (branch) → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 |
| On feature branch, tree dirty                    | Step 3 (commit) → 4 → 5 → 6 → 7 → 8 → 9 → 10 |
| On feature branch, tree clean, branch unpushed   | Step 4 (push) → 5 → 6 → 7 → 8 → 9 → 10 |
| On feature branch, tree clean, pushed, no PR     | Step 5 (open PR) → 6 → 7 → 8 → 9 → 10 |
| On feature branch, tree clean, pushed, PR open   | Step 6 → 7 → 8 → 9 → 10 |
| Detached HEAD, or PR state ≠ OPEN                | Stop and explain |

Tell Alex which steps you're about to run before running them — a
single line like "you're on main with unpushed changes; I'll branch,
commit, push, open a PR, then merge" lets him course-correct early.

## Step 2: create a feature branch (only if currently on `main`)

Run only when Step 1 detected "on `main` with uncommitted changes".

Auto-generate a branch name from the dirty changes — pick the most
likely conventional-commit type (`git diff --stat` + a quick read of
the changed files tells you whether it's `feat`, `fix`, `chore`,
`docs`, `test`, etc.) and a short hyphenated subject. Project
convention is flat names: `feat-ats-dispatcher`, not
`feature/ats-dispatcher`. Examples: `chore-rename-merge-skill`,
`fix-jina-captcha-retry`, `docs-update-claude-md`.

Show Alex the proposed name and the changed-files summary, then
proceed unless he renames:

```sh
git switch -c <branch-name>
```

Do not stash or move work between trees — `git switch -c` carries the
uncommitted changes onto the new branch automatically.

## Step 3: commit the dirty tree (only if uncommitted changes)

Run only when there are uncommitted changes after Step 2. Follow the
`/commit` skill's logic: one logical change per commit, conventional
type, short imperative subject, body explains *why* if non-obvious.

Quick recap of the must-haves (the full skill is in
`.claude/skills/commit/SKILL.md`):

- Stage explicitly with file names — never `git add -A` or `git add .`
  (risks pulling in pre-staged or unrelated work).
- Subject under ~50 chars, imperative mood, lowercase, no trailing
  period.
- Conventional type that matches the change shape: `feat` for new
  behavior, `fix` for bugs in existing behavior, `refactor` for
  no-behavior-change restructure, `chore` for tooling/skills/config,
  `docs`, `test`, `style`, `perf`, `ci`, `build`, `revert`.
- Multiple `-m` flags for subject + body + trailers. Keep the body
  empty when the subject is enough.
- The `commit-msg` lefthook enforces conventional format; the
  `pre-commit` hook runs lint/test/typecheck. **Never** use
  `--no-verify`. If a hook fails, fix the cause and create a NEW
  commit (do not amend).

If the dirty tree spans multiple unrelated logical changes, split into
multiple commits. Preview the planned commit(s) to Alex before any
`git commit` runs — one line per commit with the subject and the
files. Proceed once he OKs.

If there were already-staged files at the start of the run that don't
belong to this work, stop and ask. Don't sweep them into the ship
flow.

## Step 4: push the branch if needed

Run when the branch has unpushed commits or no upstream.

```sh
# No upstream → first push
git push -u origin "$(git branch --show-current)"

# Has upstream, ahead → push the new commits
git push
```

If the branch's upstream is behind a rebase that hasn't been
force-pushed, `git push` will reject. Surface the conflict; offer
`git push --force-with-lease` only if Alex asked for the rebase
explicitly. Don't force-push silently.

## Step 5: open a PR if needed

Run when no PR exists for the branch.

Use `gh pr create --fill` to seed the PR title and body from the
branch's commits. `--fill` uses the latest commit's subject as the
title and the bulleted commit list as the body, which is exactly what
this repo's squash-merge release-note flow expects.

```sh
GITHUB_TOKEN= gh pr create --fill --base main 2>&1
```

Capture the new PR's number from the output URL. If `--fill` would
produce a non-conventional subject (rare — depends on the latest
commit), proceed anyway — Step 7 lets Alex rewrite the title before
merging.

## Step 6: pre-flight — rebased on main and CI green

The merge happens server-side, but the branch state is what Alex has
been testing. Make sure it's current and the checks are green.

```sh
git fetch origin main
git log --oneline HEAD..origin/main         # commits on main not in this branch
gh pr checks <number>
```

- If `HEAD..origin/main` shows any commits, the branch is **not**
  rebased on main. Tell Alex; offer to rebase locally and force-push,
  or proceed if he overrides. Do not silently merge a stale branch.
- All required checks passing → continue.
- Failing or pending → show the output and ask Alex whether to wait,
  fix, or override. Do not merge a red PR without an explicit "merge
  anyway".
- If a check is `pending`, prefer waiting over override. Use Bash with
  `run_in_background` and an `until`-loop that exits when no checks are
  pending — you'll get one notification when CI lands:

  ```sh
  until gh pr checks <n> --json bucket --jq 'all(.bucket != "pending")' \
        2>/dev/null | grep -q true; do
    sleep 20
  done
  gh pr checks <n>
  ```

## Step 7: preview the squash commit (= release note)

The squash commit title and body become:

1. The single commit landed on `main`.
2. The line in the auto-generated GitHub Release notes (via
   `generate_release_notes: true` in `release.yml`).

By default `gh pr merge --squash` uses `<PR title> (#<PR number>)` as
the subject and the bulleted list of branch commits as the body.

Show Alex the preview:

```
Squash subject (release-note line):
  <PR title> (#<PR number>)

Squash body:
  * <commit 1 subject>
  * <commit 2 subject>
  * ...
```

Get the body preview from:

```sh
git log --reverse --pretty=format:'* %s' origin/main..HEAD
```

Ask Alex:

- "OK to ship as-is?"
- "Want to edit the PR title first?" Lefthook's `commit-msg` only runs
  locally, so nothing enforces the conventional-commits format on the
  squash commit itself — keep it conventional anyway, since it becomes
  the release-note line.
- "Want to rewrite the body?" (rare — only if the per-commit subjects
  would read badly in `git log` on main).

If Alex wants to edit the title, use the REST API (see auth-quirk
section above — `gh pr edit` hits the deprecated Projects-classic
GraphQL):

```sh
GITHUB_TOKEN= gh api -X PATCH /repos/<owner>/<repo>/pulls/<n> \
  -f title="<new title>" --jq '.title'
```

Then re-show the preview before continuing.

## Step 8: merge

Once Alex approves:

```sh
GITHUB_TOKEN= gh pr merge <number> --squash --delete-branch
```

`--delete-branch` removes both local and remote branches after the
merge succeeds. Do not pass `--auto` — Alex has already given explicit
approval; let the merge happen now.

If the merge fails (mergeability check, conflict, branch protection),
report the failure and stop. Do not retry blindly.

## Step 9: wait for the release workflow

`release.yml` runs on push to `main`. Run an `until`-loop in
background — one notification when the run finishes, no polling churn:

```sh
until gh run list --workflow=release.yml --branch=main --limit=1 \
        --json status --jq '.[0].status' 2>/dev/null | grep -q completed; do
  sleep 10
done
gh run list --workflow=release.yml --branch=main --limit=1 \
  --json databaseId,status,conclusion,url,createdAt
```

Cap the wait at ~3 minutes — if it hasn't finished by then, report the
run's URL and let Alex check manually.

Acceptable conclusions:

- `success` — proceed to Step 10.
- `failure` / `cancelled` — report the run URL, stop. The release
  didn't cut. Alex can re-run or investigate.

## Step 10: surface the release

Once the run is `success`:

```sh
gh release list --limit 1 --json tagName,name,createdAt,isLatest
gh release view --json url --jq '.url'
```

(`gh release list` does not expose `url` directly — the per-release
view does.)

Report to Alex:

```
✓ Created branch <branch>         (only if Step 2 fired)
✓ Committed N change(s):          (only if Step 3 fired)
    <type>: <subject>
    ...
✓ Pushed branch <branch>          (only if Step 4 fired)
✓ Opened PR #<number>: <title>    (only if Step 5 fired)
✓ Merged PR #<number>: <title>
✓ Release workflow succeeded: <run URL>
✓ New release: <tagName>
  <release URL>
```

Mention the tag (e.g. `v2026.05.02`) explicitly so Alex sees what the
CalVer logic chose. Skip any line whose step didn't run, so the
summary reflects what actually happened.

## Notes

- **Never** bypass branch protections, force-merge, or use `--admin`
  unless Alex explicitly asks.
- **Never** skip the release workflow check — Alex needs to know if
  the release failed even after the merge succeeded.
- The release commit (`release: v<version>`) is excluded from triggering
  another release by the workflow's `if:` guard. Don't worry about loops.
- Releasing publishes a tag and GitHub Release page. That's a public,
  visible change — treat it as such. If Alex says "ship" but seems
  unsure, ask once.
- The skill handles uncommitted work as part of the flow (Step 3),
  but it follows the `/commit` skill's atomic-conventional-commit
  rules — preview the planned commits before running them, split
  unrelated changes, never `--no-verify`. If the tree contains work
  that doesn't belong in this ship (pre-staged unrelated files,
  half-finished experiments alongside the actual change), stop and
  ask before bundling them in.
