import { z } from "zod/v4";
import {
  type CompanyExclusion,
  type JobLedger,
  PROCESSED_JOB_OUTCOMES,
  type ProcessedJob,
} from "./jobLedger";
import {
  createCompanyExclusionWriteRecord,
  createProcessedJobWriteRecord,
  normalizeJobLedgerText,
} from "./jobLedgerRecord";
import {
  EXCLUDE_COMPANY_SQL,
  FIND_BY_RAW_URL_SQL,
  FIND_COMPANY_EXCLUSION_SQL,
  HAS_MIGRATION_SQL,
  MARK_MIGRATION_SQL,
  NOTION_BACKFILL_STATS_SQL,
  RECORD_PROCESSED_JOB_SQL,
  TITLES_FOR_COMPANY_SQL,
} from "./jobLedgerSql";

type D1Value = string | null;

interface D1PreparedStatement {
  bind(...values: D1Value[]): D1PreparedStatement;
  first(): Promise<unknown>;
  all(): Promise<unknown>;
  run(): Promise<unknown>;
}

interface D1DatabaseBinding {
  prepare(query: string): D1PreparedStatement;
}

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

const TitleRowsResultSchema = z.object({
  success: z.literal(true),
  results: z.array(z.object({ title: z.string() })),
});

const NotionBackfillStatsSchema = z.object({
  sourceRows: z.number(),
  urls: z.number(),
  companyTitlePairs: z.number(),
  urlLessRows: z.number(),
  exclusions: z.number(),
});

const MigrationRowSchema = z.object({ name: z.string() });
const WriteResultSchema = z.object({ success: z.literal(true) });

export function createD1JobLedger(binding: D1DatabaseBinding): JobLedger {
  return {
    async findByRawUrl(rawUrl) {
      const row = ProcessedJobRowSchema.nullable().parse(
        await binding.prepare(FIND_BY_RAW_URL_SQL).bind(rawUrl).first(),
      );
      return row ? toProcessedJob(row) : null;
    },
    async titlesForCompany(company) {
      const result = TitleRowsResultSchema.parse(
        await binding.prepare(TITLES_FOR_COMPANY_SQL).bind(normalizeJobLedgerText(company)).all(),
      );
      return result.results.map((row) => row.title);
    },
    async findCompanyExclusion(company) {
      const row = CompanyExclusionRowSchema.nullable().parse(
        await binding
          .prepare(FIND_COMPANY_EXCLUSION_SQL)
          .bind(normalizeJobLedgerText(company))
          .first(),
      );
      return row ? toCompanyExclusion(row) : null;
    },
    async recordProcessedJob(input) {
      const record = createProcessedJobWriteRecord(input);
      WriteResultSchema.parse(
        await binding
          .prepare(RECORD_PROCESSED_JOB_SQL)
          .bind(
            record.sourceKey,
            record.rawUrl,
            record.company,
            record.normalizedCompany,
            record.title,
            record.normalizedTitle,
            record.outcome,
            record.firstProcessedAt,
            record.lastProcessedAt,
            record.traceId,
          )
          .run(),
      );
    },
    async excludeCompany(input) {
      const record = createCompanyExclusionWriteRecord(input);
      WriteResultSchema.parse(
        await binding
          .prepare(EXCLUDE_COMPANY_SQL)
          .bind(record.normalizedCompany, record.company, record.excludedAt, record.sourceKey)
          .run(),
      );
    },
    async notionBackfillStats() {
      const stats = NotionBackfillStatsSchema.nullable().parse(
        await binding.prepare(NOTION_BACKFILL_STATS_SQL).first(),
      );
      if (!stats) throw new Error("Could not read Notion backfill statistics");
      return stats;
    },
    async markMigration(name, completedAt) {
      WriteResultSchema.parse(
        await binding.prepare(MARK_MIGRATION_SQL).bind(name, completedAt).run(),
      );
    },
    async hasMigration(name) {
      return (
        MigrationRowSchema.nullable().parse(
          await binding.prepare(HAS_MIGRATION_SQL).bind(name).first(),
        ) !== null
      );
    },
    async close() {},
  };
}

function toProcessedJob(row: z.infer<typeof ProcessedJobRowSchema>): ProcessedJob {
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

function toCompanyExclusion(row: z.infer<typeof CompanyExclusionRowSchema>): CompanyExclusion {
  return { company: row.company, excludedAt: row.excluded_at };
}
