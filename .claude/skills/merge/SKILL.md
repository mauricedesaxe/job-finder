---
name: merge
description: Squash-merge a pull request with awareness that the merge triggers a release. Previews the squash commit title and body (which become a release-note line and the main-branch commit), waits for explicit approval, performs the merge, then surfaces the resulting CalVer tag and GitHub Release URL once the release workflow completes. Use when Alex says "merge", "/merge", "merge this PR", "ship it", or otherwise wants the current branch landed on main.
---

# Merge Skill

Squash-merge a PR with full release awareness. **Every merge to `main`
triggers `.github/workflows/release.yml`**, which cuts a CalVer tag and a
GitHub Release whose notes come from the squash commit title. Treat the
squash title like a release note, because that is what it becomes.

## Step 1: identify the PR

`$ARGUMENTS` may be empty, a PR number (e.g. `42`), or a PR URL.

- **Empty** — find the PR for the current branch:

  ```sh
  git branch --show-current
  gh pr view --json number,title,headRefName,state,mergeable,mergeStateStatus
  ```

  If there is no open PR for the current branch, say so and stop.

- **Number or URL** — `gh pr view <ref> --json ...` directly.

Capture: PR number, PR title, head branch, base branch, state.

If state is not `OPEN`, stop and explain (already merged, closed, draft).

## Step 2: pre-flight — clean working tree and rebased on main

The merge happens server-side, but the local branch is what Alex has been
testing. Make sure it matches.

```sh
git status --short
git fetch origin main
git log --oneline HEAD..origin/main         # commits on main not in this branch
```

- If `git status` shows uncommitted changes, stop and ask Alex to handle
  them (commit via `/commit`, stash, or discard).
- If `HEAD..origin/main` shows any commits, the branch is **not** rebased
  on main. Tell Alex; offer to rebase locally and force-push, or proceed
  if he overrides. Do not silently merge a stale branch.

## Step 3: pre-flight — CI green

```sh
gh pr checks <number>
```

- All required checks passing → continue.
- Failing or pending → show the output and ask Alex whether to wait, fix,
  or override. Do not merge a red PR without an explicit "merge anyway"
  from him.
- "Override and merge" is a valid choice he can make — don't argue, but
  do confirm once.

## Step 4: preview the squash commit (= release note)

The squash commit title and body become:

1. The single commit landed on `main`.
2. The line in the auto-generated GitHub Release notes (via
   `generate_release_notes: true` in `release.yml`).

By default `gh pr merge --squash` uses `<PR title> (#<PR number>)` as the
subject and the bulleted list of branch commits as the body.

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

- "OK to merge as-is?"
- "Want to edit the PR title first?" (lefthook's `commit-msg` only runs
  locally, so nothing enforces the conventional-commits format on the
  squash commit itself — keep it conventional anyway, since it becomes
  the release-note line.)
- "Want to rewrite the body?" (rare — only if the per-commit subjects
  would read badly in `git log` on main).

If Alex wants to edit the title:

```sh
gh pr edit <number> --title "<new title>"
```

Then re-show the preview before continuing.

## Step 5: merge

Once Alex approves:

```sh
gh pr merge <number> --squash --delete-branch
```

`--delete-branch` removes both local and remote branches after the merge
succeeds. Do not pass `--auto` here — Alex has already given explicit
approval; let the merge happen now.

If the merge fails (mergeability check, conflict, branch protection),
report the failure and stop. Do not retry blindly.

## Step 6: wait for the release workflow

`release.yml` runs on push to `main`. Use the `Monitor` tool with an
until-loop so the harness fires one notification when the run finishes,
instead of polling-and-cache-thrashing through repeated `Bash` calls:

```sh
until gh run list --workflow=release.yml --branch=main --limit=1 \
        --json status --jq '.[0].status' | grep -q completed; do
  sleep 10
done
gh run list --workflow=release.yml --branch=main --limit=1 \
  --json databaseId,status,conclusion,url,createdAt
```

Cap the wait at ~3 minutes — if it hasn't finished by then, report the
run's URL and let Alex check manually.

Acceptable conclusions:

- `success` — proceed to step 7.
- `failure` / `cancelled` — report the run URL, stop. The release didn't
  cut. Alex can re-run or investigate.

## Step 7: surface the release

Once the run is `success`:

```sh
gh release list --limit 1 --json tagName,name,url,createdAt
```

Report to Alex:

```
✓ Merged PR #<number>: <title>
✓ Release workflow succeeded: <run URL>
✓ New release: <tagName>
  <release URL>
```

Mention the tag (e.g. `v2026.04.30`) explicitly so Alex sees what the
CalVer logic chose.

## Notes

- **Never** bypass branch protections, force-merge, or use `--admin`
  unless Alex explicitly asks.
- **Never** skip the release workflow check — Alex needs to know if the
  release failed even after the merge succeeded.
- The release commit (`release: v<version>`) is excluded from triggering
  another release by the workflow's `if:` guard. Don't worry about loops.
- If `gh` is unauthenticated, this skill cannot work. Say so and stop.
- Releasing publishes a tag and GitHub Release page. That's a public,
  visible change — treat it as such. If Alex says "merge" but seems
  unsure, ask once.
