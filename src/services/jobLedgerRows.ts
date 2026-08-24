import { z } from "zod/v4";
import {
  type CompanyExclusion,
  type NotionBackfillStats,
  PROCESSED_JOB_OUTCOMES,
  type ProcessedJob,
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

const NotionBackfillStatsSchema = z.object({
  sourceRows: z.number().int().nonnegative(),
  urls: z.number().int().nonnegative(),
  companyTitlePairs: z.number().int().nonnegative(),
  urlLessRows: z.number().int().nonnegative(),
  exclusions: z.number().int().nonnegative(),
});

const MigrationRowSchema = z.object({ name: z.string() });

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

export function parseNotionBackfillStats(value: unknown): NotionBackfillStats | null {
  return NotionBackfillStatsSchema.nullable().parse(value);
}

export function parseMigrationRow(value: unknown): boolean {
  return MigrationRowSchema.nullable().parse(value) !== null;
}
