import type { JobLedger, ProcessedJobOutcome } from "../services/jobLedger";
import type { JobListing } from "../types";

export async function recordTerminalResult({
  ledger,
  url,
  job,
  outcome,
  traceId,
  project,
}: {
  ledger: JobLedger;
  url: string;
  job: Pick<JobListing, "company" | "title">;
  outcome: ProcessedJobOutcome;
  traceId: string;
  project?: () => Promise<unknown>;
}): Promise<void> {
  ledger.recordProcessedJob({
    rawUrl: url,
    company: job.company,
    title: job.title,
    outcome,
    traceId,
  });
  await project?.();
}
