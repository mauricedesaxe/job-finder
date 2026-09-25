# Job Finder

Job Finder discovers job listings, evaluates them against an owner's preferences, and records the evidence needed to reproduce each decision.

## Search policy

**Search setup**:
The owner-facing workflow for managing acquisition and qualification. It is not one persisted policy.
_Avoid_: Search configuration

**Acquisition policy**:
The ordered keywords and sources that determine which job listings the system seeks.
_Avoid_: Search configuration, discovery configuration

**Qualification definition**:
The ordered personal criteria and target profiles that express which jobs the owner wants.
_Avoid_: Search configuration, prompt release

## Execution policy

**Prompt release**:
An immutable set of executable prompts used to evaluate and enrich job listings.
_Avoid_: Qualification definition

**Relevance release**:
An immutable execution policy that identifies the relevance implementation and its serving parameters.
_Avoid_: Qualification definition

**Release target**:
The exact prompt release and relevance release evaluated, promoted, and executed together.
_Avoid_: Qualification definition, active configuration

**Qualification target**:
The exact input-preparation, relevance, enrichment, and deduplication component releases promoted, activated, and executed together.
_Avoid_: Qualification definition, independent phase targets

**Implementation artifact**:
A content-addressed executable build whose runtime, dependencies, entrypoints, and owned source are fixed.
_Avoid_: Implementation label, source-file hash

**Experiment input**:
The immutable manifest, exchange rates, and provider settings shared by benchmark runs that may be compared.
_Avoid_: Manifest

## Lifecycle

**Publication**:
An immutable record that makes an acquisition policy or qualification definition eligible for evaluation and activation.
_Avoid_: Activation

**Active acquisition policy**:
The published acquisition policy selected for new discovery runs.
_Avoid_: Active search configuration

**Active release target**:
The promoted release target selected for new qualification work.
_Avoid_: Active qualification definition

**Active qualification target**:
The promoted composite qualification target selected atomically for new qualification work.
_Avoid_: Active relevance target, active enrichment target, active deduplication target
