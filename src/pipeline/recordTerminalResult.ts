import type {
  JobLedger,
  PendingNotionProjectionInput,
  ProcessedJobOutcome,
} from "../services/jobLedger";
import type { ResilientNotionClient } from "../services/notion";
import type { JobListing, JobStatus } from "../types";
import { projectPendingNotionProjection } from "./notionProjection";

export async function recordTerminalResult({
  ledger,
  job,
  outcome,
  traceId,
  projection,
}: {
  ledger: JobLedger;
  job: JobListing;
  outcome: ProcessedJobOutcome;
  traceId: string;
  projection?: {
    notion: ResilientNotionClient;
    databaseId: string;
    status: JobStatus;
  };
}): Promise<void> {
  const pendingNotionProjection: PendingNotionProjectionInput | undefined = projection
    ? {
        job,
        status: projection.status,
        createdAt: new Date().toISOString(),
      }
    : undefined;

  const storedProjection = await ledger.recordProcessedJob({
    rawUrl: job.url,
    company: job.company,
    title: job.title,
    outcome,
    traceId,
    pendingNotionProjection,
  });

  if (storedProjection && projection) {
    await projectPendingNotionProjection({
      ledger,
      notion: projection.notion,
      databaseId: projection.databaseId,
      projection: storedProjection,
    });
  }
}
