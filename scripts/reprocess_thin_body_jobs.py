"""Requeue jobs that were decided against broken scrapes the ATS backfill repaired.

Thin scrapes (Jina intermittently returns HTTP 200 with an empty body on
JS-rendered ATS pages) produced decisions made with no usable posting text.
Deleting those decisions and resetting the work items to pending makes the
regular queue drain re-scrape every selected job and decide it again under
the current prompt release, now composing the canonical ATS description and
structured compensation. Decisions quoted by review items are left alone, as
are jobs holding any other decision.
"""

from __future__ import annotations

from job_finder.pipeline.reprocess import reset_thin_body_jobs, select_thin_body_jobs
from scripts.reprocess_jobs import run_reprocess_command


def main() -> None:
    run_reprocess_command(
        "Requeue jobs decided against thin scrapes that the ATS backfill repaired",
        select_thin_body_jobs,
        reset_thin_body_jobs,
    )


if __name__ == "__main__":
    main()
