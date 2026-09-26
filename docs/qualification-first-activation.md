# Activate the first qualification target

Use this procedure after an operator has [prepared canonical evidence](qualification-evidence-operator.md)
for all five phases. A fresh owner instance has no active qualification target.
The first promotion therefore has no baseline target or relevance comparison.

1. Open `/configuration/qualification-promotion`. Confirm that the active
   target is `none` and note its generation. Choose **No active baseline (first
   activation)** and the candidate created from the owner's published setup.
2. Select the passed canonical evidence ID for input preparation, relevance,
   enrichment, deduplication, and composition. Leave the relevance comparison
   empty. Preview the promotion. Resolve every reported failure before you
   record a decision.
3. Review the selected evidence and the preview with the owner. Approve the
   candidate with a reason that identifies what was checked. The page records
   an immutable decision and shows its ID.
4. Select that approved decision in the activation form. The form includes the
   active target and generation you just observed. Activate, then reload the
   page and confirm that the active target is the candidate and the generation
   increased. If the page reports that the active target changed, reload and
   review the current state before retrying.

Scheduled qualification can use the candidate only after the active target
changes. A later promotion uses the current active target as its baseline and
must satisfy the comparison rules in
[Release coverage and promotion evidence](release-coverage-decision.md).
