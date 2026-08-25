import type { JobListing, JobStatus } from "../types";

export const PROCESSED_JOB_OUTCOMES = [
  "historical",
  "inserted",
  "rejected",
  "duplicated",
  "companyApplied",
  "archived",
] as const;

export type ProcessedJobOutcome = (typeof PROCESSED_JOB_OUTCOMES)[number];

export interface ProcessedJob {
  sourceKey: string;
  rawUrl: string | null;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  firstProcessedAt: string;
  lastProcessedAt: string;
  traceId: string | null;
}

export interface PendingNotionProjectionInput {
  job: JobListing;
  status: JobStatus;
  createdAt: string;
}

export type PendingNotionProjection = PendingNotionProjectionInput & {
  sourceKey: string;
};

export interface PendingReviewProjectionInput {
  traceId: string;
  createdAt: string;
}

export type PendingReviewProjection = PendingReviewProjectionInput & {
  sourceKey: string;
};

export type PendingJobProjectionInput =
  | { kind: "notion"; notion: PendingNotionProjectionInput }
  | {
      kind: "notion-and-review";
      notion: PendingNotionProjectionInput;
      review: PendingReviewProjectionInput;
    };

export type PendingJobProjections =
  | { kind: "none" }
  | { kind: "notion"; notion: PendingNotionProjection }
  | {
      kind: "notion-and-review";
      notion: PendingNotionProjection;
      review: PendingReviewProjection;
    };

export interface CompanyExclusion {
  company: string;
  excludedAt: string;
}

export interface RecordProcessedJobInput {
  rawUrl?: string;
  sourceKey?: string;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  processedAt?: string;
  traceId?: string;
  projections?: PendingJobProjectionInput;
}

export interface ExcludeCompanyInput {
  company: string;
  excludedAt?: string;
  sourceKey?: string;
}

export interface NotionBackfillStats {
  sourceRows: number;
  urls: number;
  companyTitlePairs: number;
  urlLessRows: number;
  exclusions: number;
}

export interface JobLedger {
  findByRawUrl(rawUrl: string): Promise<ProcessedJob | null>;
  titlesForCompany(company: string): Promise<string[]>;
  findCompanyExclusion(company: string): Promise<CompanyExclusion | null>;
  recordProcessedJob(input: RecordProcessedJobInput): Promise<PendingJobProjections>;
  nextPendingNotionProjectionBatch(): Promise<PendingNotionProjection[]>;
  markNotionProjectionComplete(sourceKey: string): Promise<void>;
  listPendingReviewProjections(): Promise<PendingReviewProjection[]>;
  markReviewProjectionComplete(sourceKey: string): Promise<void>;
  excludeCompany(input: ExcludeCompanyInput): Promise<void>;
  notionBackfillStats(): Promise<NotionBackfillStats>;
  markMigration(name: string, completedAt: string): Promise<void>;
  hasMigration(name: string): Promise<boolean>;
}
