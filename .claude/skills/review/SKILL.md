---
name: review
description: Run the project's two reviewer agents in parallel against the current diff and report findings in chat. Never posts to GitHub. Use when Alex says "review my changes", "/review", or otherwise wants a quality check before commit, push, or merge.
---

# Review Skill

Run the two reviewer agents in parallel against the current branch's
diff vs `main`, plus any uncommitted edits in the working tree. Collate
their findings into a single chat report. Do not post to GitHub.

The reviewers are:

- `code-reviewer` — module boundaries, naming, imports, configuration
  access, logging, error handling, idempotency, type discipline (parse
  at boundaries, discriminated unions, stringly-typed narrowing), and
  test coverage (error cases, naming, no-skipIf, body-visible fixtures,
  full-pipeline integration tests, parallel-LLM pattern, FP/FN
  threshold direction).
- `llm-quality-reviewer` — N-shot prompts, fixture content,
  FP-over-FN bias, mandatory routing through `services/llm.ts`, token
  tracking, failure modes, concurrency stacking.

## Step 1: gather the diff

```sh
git diff --name-only main...HEAD       # committed branch changes
git diff --name-only                   # unstaged
git diff --name-only --staged          # staged
```

Deduplicate into one list of changed paths. If the list is empty, say
so and stop — there is nothing to review.

## Step 2: spawn both agents in parallel

Each agent already knows its own scope. Send both tool calls in a
single message so they run concurrently. Each prompt should give the
agent:

1. The diff source — "the diff `main...HEAD` plus any uncommitted
   edits in the working tree".
2. The list of changed paths.
3. A reminder that its output is collated back into a single chat
   report, so it should be concrete (file path, line number, fix) and
   skip restating its own scope.

Don't try to pre-filter which agents to run based on which files
changed. If `llm-quality-reviewer` has no relevant files (no prompts
or fixtures touched), it will return "no issues found" — that's the
expected outcome and takes seconds.

## Step 3: collate the report

Once both agents return, assemble a single chat report:

```
# Review of <diff source>

Changed files: <count> across <areas>.

## code-reviewer
<findings, or "No issues found.">

## llm-quality-reviewer
<findings, or "No issues found.">

## Summary
- <N issues found total, or "Clean across both reviewers.">
- Top priorities: <bullet the most impactful items, if any>.
```

Keep it tight. Pull out the concrete findings — don't paste raw agent
transcripts.

## Notes

- Never post GitHub review comments. This skill produces a chat report
  only.
- If an agent errors out or returns nothing, note that under its
  section rather than silently dropping it.
- Re-running this skill is cheap and expected — Alex iterates.
