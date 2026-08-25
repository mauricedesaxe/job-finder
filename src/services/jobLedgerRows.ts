import { z } from "zod/v4";
import { JobListingSchema, JobStatusSchema } from "../types";
import {
  type CompanyExclusion,
  type PendingNotionProjection,
  type PendingReviewProjection,
  PROCESSED_JOB_OUTCOMES,
  type ProcessedJob,
  type SelectiveNotionImportStats,
} from "./jobLedger";

const ProcessedJobRowSchema = z.object({
  source_key: z.string(),
  raw_url: z.string().nullable(),
  company: z.string(),
  title: z.string(),
  outcome: z.enum(PROCESSED_JOB_OUTCOMES),
  first_processed_at: z.string(),
  last_processed_at: z.string(),
  trace_id: z.string().nullable(),
});

const CompanyExclusionRowSchema = z.object({
  company: z.string(),
  excluded_at: z.string(),
});

const TitleRowsSchema = z.array(z.object({ title: z.string() }));

const SelectiveNotionImportStatsSchema = z.object({
  blockedCompanies: z.number().int().nonnegative(),
  recentApplications: z.number().int().nonnegative(),
});

const MigrationRowSchema = z.object({ name: z.string() });

const PendingReviewProjectionRowSchema = z.object({
  source_key: z.string(),
  trace_id: z.string().min(1),
  created_at: z.iso.datetime(),
});

const PendingNotionProjectionRowSchema = z.object({
  source_key: z.string(),
  job_json: z.string(),
  status: JobStatusSchema,
  created_at: z.iso.datetime(),
});

export function parseProcessedJobRow(value: unknown): ProcessedJob | null {
  const row = ProcessedJobRowSchema.nullable().parse(value);
  if (!row) return null;

  return {
    sourceKey: row.source_key,
    rawUrl: row.raw_url,
    company: row.company,
    title: row.title,
    outcome: row.outcome,
    firstProcessedAt: row.first_processed_at,
    lastProcessedAt: row.last_processed_at,
    traceId: row.trace_id,
  };
}

export function parseCompanyExclusionRow(value: unknown): CompanyExclusion | null {
  const row = CompanyExclusionRowSchema.nullable().parse(value);
  return row ? { company: row.company, excludedAt: row.excluded_at } : null;
}

export function parseTitleRows(value: unknown): string[] {
  return TitleRowsSchema.parse(value).map((row) => row.title);
}

export function parseSelectiveNotionImportStats(value: unknown): SelectiveNotionImportStats | null {
  return SelectiveNotionImportStatsSchema.nullable().parse(value);
}

export function parseMigrationRow(value: unknown): boolean {
  return MigrationRowSchema.nullable().parse(value) !== null;
}

export function parsePendingNotionProjectionRows(value: unknown): PendingNotionProjection[] {
  return z
    .array(PendingNotionProjectionRowSchema)
    .parse(value)
    .map((row) => {
      const storedJob: unknown = JSON.parse(row.job_json);
      return {
        sourceKey: row.source_key,
        job: JobListingSchema.parse(storedJob),
        status: row.status,
        createdAt: row.created_at,
      };
    });
}

export function parsePendingReviewProjectionRows(value: unknown): PendingReviewProjection[] {
  return z
    .array(PendingReviewProjectionRowSchema)
    .parse(value)
    .map((row) => ({
      sourceKey: row.source_key,
      traceId: row.trace_id,
      createdAt: row.created_at,
    }));
}
