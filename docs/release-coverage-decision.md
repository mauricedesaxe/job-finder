# Release coverage and promotion evidence

Status: Proposed for owner approval. Bead: `job-finder-rac.6.2`.

## Current contract

`ReleaseTarget` pairs a prompt release with a relevance release. A prompt release
contains filter, profile, enrichment, and title-deduplication prompts. A
relevance release identifies the provider, model, composition rules, and SHA-256
bytes of selected relevance source files. `validate_release_target` checks those
source bytes against the files in the running checkout.

The PostgreSQL manifest runner calls `evaluate_job` for each curated case. It
uses only filter and profile prompt versions. It does not run ATS enrichment,
structural rejection, enrichment prompts, title deduplication, or the stateful
production work-item path. The comparison and promotion decision therefore
measure relevance outcomes on the frozen manifest. They do not certify the
complete production qualification path or changes to the other prompt phases.
`implementation_ref` is stored with a run, but callers supply it as an arbitrary
string. It is not verified against the checkout or deployment image.

## Decision

Treat the existing paired target as a legacy target whose promotion gate covers
**relevance evaluation only**. Keep its stored IDs and activation receipts
unchanged. Do not describe a passing manifest comparison as approval of ATS,
structural, enrichment, deduplication, or end-to-end production behavior.

For new lifecycle work, use separate immutable, independently activated targets
for relevance, enrichment, and title deduplication. The relevance target owns
filter and profile prompt versions, execution policy, and the source artifacts
that actually execute those versions. Enrichment and deduplication need their own
evidence and promotion rules before their activations are gated. ATS and
structural policy are separate deterministic qualification inputs, pinned on a
production run and tested by their own fixtures. A single owner-facing setup
screen may edit several drafts, but it does not imply one persisted release or
one shared approval gate.

A relevance promotion compares a baseline and candidate on the same immutable
manifest and records the exact target and verified implementation identity for
each run. The implementation identity must be derived from the executing build
and the relevant source/dependency closure, not a caller-supplied label.
Historical `implementation_ref` values remain labels; do not reinterpret them
as verified identities. Any future full-path gate needs a distinct manifest and
result type that actually executes the full path.

## Migration order

1. Inventory active and historical target IDs, release policies, source
   entrypoints, and activation receipts. Preserve every stored digest and
   historical run reference.
2. Add the new target and verified implementation identities beside the legacy
   rows. Backfill only facts that can be proven from stored data; leave unknown
   historical implementation identities explicitly unknown.
3. Keep the existing hashed serving files byte-for-byte intact while an active
   or replayable legacy target references them. Put the new executor at a new
   entrypoint, hash its actual source, and dispatch both benchmark and production
   execution by the stored target identity. Benchmark that candidate against a
   baseline on one frozen manifest, then activate it through a checked
   promotion. The benchmark and production calls for a target must resolve to
   the same implementation. The old files remain executable implementations for
   old targets, not import-only compatibility facades.
4. Migrate internal callers to the new subject modules without re-exports.
   Move or delete an old hashed file only after no active or replayable target
   requires its original path and bytes, or after an immutable versioned
   implementation provides the exact persisted artifact. Never rewrite an old
   digest to make a move look unchanged.
5. Update budget estimates and run provenance from the exact pinned acquisition
   policy and release targets. A configuration-only activation must not change
   the estimated model calls of an unchanged relevance target.

This order means the `evaluate.py`, `jev.py`, and `openrouter.py` byte-preservation
rule remains in force for package-only moves. The job-model move in
`job-finder-rac.2.1` cannot delete `job_finder.jobs.models` while `evaluate.py`
still imports it. It must either wait for the versioned-executor migration or
retain the old model as a real legacy implementation dependency until the
persisted target is retired. No internal caller gets a compatibility re-export.

## Rejected choices

Keeping the combined target indefinitely lets a relevance-only benchmark appear
to approve changes to enrichment and deduplication. Expanding this manifest into
a full production gate would require raw discovery inputs, ATS and structural
outcomes, stateful work transitions, and separate expected results. Calling the
current manifest a full-path gate would make its evidence misleading.
