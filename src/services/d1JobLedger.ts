import { z } from "zod/v4";
import type { JobLedger } from "./jobLedger";
import {
  companyExclusionWriteValues,
  createCompanyExclusionWriteRecord,
  createProcessedJobWriteRecord,
  normalizeJobLedgerText,
  processedJobWriteValues,
} from "./jobLedgerRecord";
import {
  parseCompanyExclusionRow,
  parseMigrationRow,
  parseNotionBackfillStats,
  parseProcessedJobRow,
  parseTitleRows,
} from "./jobLedgerRows";
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

const TitleRowsResultSchema = z.object({
  success: z.literal(true),
  results: z.array(z.unknown()),
});

const WriteResultSchema = z.object({ success: z.literal(true) });

export function createD1JobLedger(binding: D1DatabaseBinding): JobLedger {
  return {
    async findByRawUrl(rawUrl) {
      return parseProcessedJobRow(await binding.prepare(FIND_BY_RAW_URL_SQL).bind(rawUrl).first());
    },
    async titlesForCompany(company) {
      const result = TitleRowsResultSchema.parse(
        await binding.prepare(TITLES_FOR_COMPANY_SQL).bind(normalizeJobLedgerText(company)).all(),
      );
      return parseTitleRows(result.results);
    },
    async findCompanyExclusion(company) {
      return parseCompanyExclusionRow(
        await binding
          .prepare(FIND_COMPANY_EXCLUSION_SQL)
          .bind(normalizeJobLedgerText(company))
          .first(),
      );
    },
    async recordProcessedJob(input) {
      const record = createProcessedJobWriteRecord(input);
      WriteResultSchema.parse(
        await binding
          .prepare(RECORD_PROCESSED_JOB_SQL)
          .bind(...processedJobWriteValues(record))
          .run(),
      );
    },
    async excludeCompany(input) {
      const record = createCompanyExclusionWriteRecord(input);
      WriteResultSchema.parse(
        await binding
          .prepare(EXCLUDE_COMPANY_SQL)
          .bind(...companyExclusionWriteValues(record))
          .run(),
      );
    },
    async notionBackfillStats() {
      const stats = parseNotionBackfillStats(
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
      return parseMigrationRow(await binding.prepare(HAS_MIGRATION_SQL).bind(name).first());
    },
  };
}
