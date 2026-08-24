import type {
  JobLedger,
  PendingNotionProjectionInput,
  ProcessedJobOutcome,
} from "../services/jobLedger";
import type { ResilientNotionClient } from "../services/notion";
import type { JobListing, JobStatus } from "../types";
import { projectPendingNotionProjection } from "./notionProjection";

export type TerminalProcessedJobOutcome = Exclude<ProcessedJobOutcome, "historical">;
type ProjectedTerminalOutcome = Exclude<TerminalProcessedJobOutcome, "duplicated">;

interface ProjectionTransport {
  notion: ResilientNotionClient;
  databaseId: string;
}

interface PreparedTerminalResultBase {
  ledger: JobLedger;
  job: JobListing;
}

export type PreparedTerminalResultInput = PreparedTerminalResultBase &
  (
    | { outcome: "duplicated"; projection?: never }
    | { outcome: ProjectedTerminalOutcome; projection: ProjectionTransport }
  );

export type RecordTerminalResultInput = PreparedTerminalResultInput & { traceId: string };

const NOTION_STATUS_BY_OUTCOME = {
  inserted: "To Review",
  rejected: "Auto-Rejected",
  companyApplied: "Company Applied",
  archived: "Archived",
  duplicated: null,
} satisfies Record<TerminalProcessedJobOutcome, JobStatus | null>;

export async function recordTerminalResult(input: RecordTerminalResultInput): Promise<void> {
  const { ledger, job, outcome, traceId } = input;
  const status = NOTION_STATUS_BY_OUTCOME[outcome];
  const pendingNotionProjection: PendingNotionProjectionInput | undefined = status
    ? { job, status, createdAt: new Date().toISOString() }
    : undefined;

  const storedProjection = await ledger.recordProcessedJob({
    rawUrl: job.url,
    company: job.company,
    title: job.title,
    outcome,
    traceId,
    pendingNotionProjection,
  });

  if (outcome === "duplicated") return;
  if (!storedProjection) {
    throw new Error("Job ledger did not return the pending Notion projection");
  }
  await projectPendingNotionProjection({
    ledger,
    notion: input.projection.notion,
    databaseId: input.projection.databaseId,
    projection: storedProjection,
  });
}
