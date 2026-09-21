"""Requeue jobs that were refused because a mis-titled scrape hid the real role.

Deleting the refusal decisions and resetting the work items to pending makes
the regular queue drain re-scrape every selected job with the reader-title
fix and decide it again. Decisions quoted by review items are left alone,
as are jobs holding any other decision. A job whose page still yields no
usable title will be refused again with the same reason and re-selected on
the next run; that churn is expected, so do not re-run to be sure.
"""

from __future__ import annotations

from job_finder.pipeline.reprocess import reset_mis_titled_jobs, select_mis_titled_jobs
from scripts.reprocess_jobs import run_reprocess_command


def main() -> None:
    run_reprocess_command(
        "Requeue jobs refused for titles that the reader-title fix repairs",
        select_mis_titled_jobs,
        reset_mis_titled_jobs,
    )


if __name__ == "__main__":
    main()
