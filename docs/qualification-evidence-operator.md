# Prepare qualification evidence for a hosted owner

Use this procedure after the owner publishes an acquisition policy and a
qualification definition, creates a candidate at
`/configuration/qualification-targets`, and enters provider credentials. The
bounded test search helps the owner inspect results, but it does not create the
five evidence records needed for qualification promotion.

The operator needs access to the owner's Railway project and its review service.
Evidence execution calls providers and can incur charges. Agree on a cost limit
with the owner and review the fixture set before running it.

## Connect to the review service

The review service exposes the qualification tools through MCP over standard
input and output. Use Railway's [SSH connection instructions](https://docs.railway.com/cli/ssh)
to register an SSH key and establish host trust. Get the service instance user
from `railway ssh config --dry-run`:

```sh
railway ssh config --project PROJECT_ID --environment production --service review --dry-run
```

Use the returned `User` value in an MCP client with this standard input and
output transport. The command runs in the deployed review container:

```json
{
  "command": "ssh",
  "args": ["-T", "USER@ssh.railway.com", "uv", "run", "--no-sync", "python", "-m", "scripts.serve_mcp"]
}
```

Check `qualification_active_get` and `qualification_candidate_get` before
preparing evidence. Record the candidate's full target ID. If the service does
not list `qualification_evidence_execute`, deploy the code containing that tool
before continuing.

## Freeze reviewed inputs

Curate cases from listings the owner has reviewed. Store one `PhaseFixtureSet`
with `qualification_fixture_set_store` for each of `input_preparation`,
`enrichment`, `deduplication`, and `composition`. Store a
`RelevanceExperimentInput` with `qualification_relevance_input_store` for
`relevance`. Save each returned 64-character input ID. The input models and
expected output shapes are defined in
`job_finder/benchmarks/qualification_evidence.py` and the corresponding
`*_execution.py` modules. The repository's raw scrape samples are not curated
qualification fixtures.

Include direct and ATS input paths. The composition fixture set must exercise
direct, ATS, qualified, rejected, retry, relevance, enrichment, and deduplication
behavior. `qualification_promotion_preview` reports missing coverage. Review
expected results with the owner before freezing a fixture set, since stored
inputs are immutable.

## Execute and inspect each phase

Call `qualification_evidence_execute` once for each phase with the candidate
`target_id`, phase name, frozen `input_id`, and a unique `idempotency_key`. The
same request and key return the recorded result on retry. If a request failed,
fix its cause and use a new key. Reusing the failed key does not call the
provider again.

For a one-shot call in the container, use the same four arguments with the CLI:

```sh
uv run --no-sync python -m scripts.execute_qualification_evidence \
  --idempotency-key OWNER-CANDIDATE-PHASE-ATTEMPT \
  --target-id CANDIDATE_TARGET_ID \
  --phase input_preparation \
  --input-id FROZEN_INPUT_ID
```

For each completed execution, read its `evidence_id` with
`qualification_evidence_get`. Keep the ID only when `origin` is `canonical` and
`outcome` is `passed`. Inspect failures and provider attempts before deciding
whether to correct a fixture or candidate. A failed or imported record cannot
authorize promotion.

After all five phases pass, take the five evidence IDs to the promotion preview
at `/configuration/qualification-promotion`. The preview checks that the records
belong to this candidate and executing build. The accepted coverage rules are
in [Release coverage and promotion evidence](release-coverage-decision.md).
