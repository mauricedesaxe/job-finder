# Advanced search configuration

Job Finder stores search setup in PostgreSQL. Browser and MCP operations use
the same typed configuration service. Neither interface edits source files.

## Lifecycle

The configuration draft is mutable and has a version number. Saving requires
the version last read by the caller. A stale save returns the current draft
instead of overwriting another edit.

Publishing freezes the saved content as a content-addressed, immutable
revision. It also compiles the revision's criteria and target profiles into an
immutable prompt release. Publishing does not activate either identity.
Idempotency keys make publication retries safe.

Configuration activation uses compare-and-swap over the active revision and
generation. A stale request returns the current active state. Only new pipeline
runs read a new activation; an existing run and its retries keep their original
configuration revision, release target, and exchange-rate snapshot.

## Configuration and release targets

The active configuration and active release target record different facts:

- The configuration revision supplies ordered search keywords and enabled job
  boards for discovery.
- The release target supplies the exact prompt and relevance-policy releases
  used for evaluation.

A search-only edit produces the same prompt release identity, so activating the
configuration changes discovery without requiring another evaluation. Editing
criteria or target profiles produces a new prompt release. Treat that release
as a candidate: evaluate it against a frozen manifest, compare it with the
active target, record an explicit approval, and activate the approved target
through compare-and-swap. Complete both activations before launching a
production run that should use the new search setup and evaluation behavior.
These activations are intentionally separate. Pause Dagster or otherwise
prevent new run creation while coordinating them so a scheduled run cannot
start with the new configuration and the previous release target.

Every orchestration run stores both identities. This is intentional: the
configuration records user-owned search intent, while an approved target may
later contain an optimized prompt derived from that intent.

## MCP operations

Use these tools for configuration changes:

1. `configuration_active_get` and `configuration_draft_get` read current state.
2. `configuration_validate` and `configuration_preview` inspect proposed
   behavior without writing.
3. `configuration_draft_update` saves with draft-version compare-and-swap.
4. `configuration_publish` freezes the saved revision and prompt release.
5. `configuration_activate` activates a published revision with active-state
   compare-and-swap.
6. `configuration_revision_list` and `configuration_revision_get` inspect
   immutable history.

For criteria or profile changes, continue with the release tools:

1. `release_target_candidate_create` identifies the candidate prompt and
   relevance policy.
2. `evaluation_run` evaluates baseline and candidate targets on the same
   manifest.
3. `release_target_compare` reports case-level differences and eligibility.
4. `release_target_decide` records explicit approval or rejection.
5. `release_target_activate` activates the exact approved candidate with
   generation-based compare-and-swap.

## Rollback

Configuration history is immutable. Roll back discovery settings by reading
the current active state, selecting an older published revision, and calling
`configuration_activate` with the current revision and generation as the
expected state. Never edit or delete the newer revision.

Executable prompt or relevance behavior has a separate approval history. To
return to an older release target, evaluate it as the candidate against the
current baseline, record a new approval, and activate that exact decision. An
old activation request may replay its historical receipt, but replay never
changes the current target. Rollback requires a new comparison, approval, and
activation request against the current baseline.

If either compare-and-swap reports changed active state, read again and decide
whether the requested rollback is still appropriate. Do not retry with guessed
generation numbers or update PostgreSQL directly.
