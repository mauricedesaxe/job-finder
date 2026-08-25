import type {
  JobLedger,
  PendingJobProjectionInput,
  ProcessedJobOutcome,
} from "../services/jobLedger";
import type { ResilientNotionClient } from "../services/notion";
import type { JobListing, JobStatus } from "../types";
import { projectPendingNotionProjection } from "./notionProjection";
import { projectPendingReviewProjection } from "./reviewProjection";

export type TerminalProcessedJobOutcome = Exclude<ProcessedJobOutcome, "historical">;
type ProjectedTerminalOutcome = Exclude<TerminalProcessedJobOutcome, "duplicated">;

interface ReviewProjectionTransport {
  enqueue(traceId: string): Promise<void>;
}

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
    | { outcome: "inserted"; projection: ProjectionTransport; review: ReviewProjectionTransport }
    | { outcome: Exclude<ProjectedTerminalOutcome, "inserted">; projection: ProjectionTransport }
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
  const createdAt = new Date().toISOString();
  const projections = pendingProjectionInput({ job, outcome, traceId, createdAt });
  const stored = await ledger.recordProcessedJob({
    rawUrl: job.url,
    company: job.company,
    title: job.title,
    outcome,
    traceId,
    projections,
  });

  switch (outcome) {
    case "duplicated":
      if (stored.kind !== "none") throw new Error("Duplicated job stored unexpected projections");
      return;
    case "inserted":
      if (stored.kind !== "notion-and-review") {
        throw new Error("Inserted job did not store both pending projections");
      }
      await projectPendingReviewProjection({
        ledger,
        projection: stored.review,
        enqueue: input.review.enqueue,
      });
      await projectPendingNotionProjection({
        ledger,
        notion: input.projection.notion,
        databaseId: input.projection.databaseId,
        projection: stored.notion,
      });
      return;
    case "rejected":
    case "companyApplied":
    case "archived":
      if (stored.kind !== "notion") {
        throw new Error(`${outcome} job did not store its pending Notion projection`);
      }
      await projectPendingNotionProjection({
        ledger,
        notion: input.projection.notion,
        databaseId: input.projection.databaseId,
        projection: stored.notion,
      });
  }
}

function pendingProjectionInput({
  job,
  outcome,
  traceId,
  createdAt,
}: {
  job: JobListing;
  outcome: TerminalProcessedJobOutcome;
  traceId: string;
  createdAt: string;
}): PendingJobProjectionInput | undefined {
  const status = NOTION_STATUS_BY_OUTCOME[outcome];
  if (!status) return undefined;
  const notion = { job, status, createdAt };
  return outcome === "inserted"
    ? { kind: "notion-and-review", notion, review: { traceId, createdAt } }
    : { kind: "notion", notion };
}
