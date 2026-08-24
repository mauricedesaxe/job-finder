import { z } from "zod/v4";
import type { JobLedger, PendingNotionProjection } from "./jobLedger";
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
  parsePendingNotionProjectionRows,
  parseProcessedJobRow,
  parseTitleRows,
} from "./jobLedgerRows";
import {
  EXCLUDE_COMPANY_SQL,
  FIND_BY_RAW_URL_SQL,
  FIND_COMPANY_EXCLUSION_SQL,
  HAS_MIGRATION_SQL,
  LIST_PENDING_NOTION_PROJECTIONS_SQL,
  MARK_MIGRATION_SQL,
  MARK_NOTION_PROJECTION_COMPLETE_SQL,
  NOTION_BACKFILL_STATS_SQL,
  RECORD_PENDING_NOTION_PROJECTION_SQL,
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

export interface D1DatabaseBinding {
  prepare(query: string): D1PreparedStatement;
  batch(statements: D1PreparedStatement[]): Promise<unknown>;
}

const RowsResultSchema = z.object({
  success: z.literal(true),
  results: z.array(z.unknown()),
});

const WriteResultSchema = z.object({ success: z.literal(true) });
const BatchWriteResultSchema = z.array(WriteResultSchema);

export function createD1JobLedger(binding: D1DatabaseBinding): JobLedger {
  return {
    async findByRawUrl(rawUrl) {
      return parseProcessedJobRow(await binding.prepare(FIND_BY_RAW_URL_SQL).bind(rawUrl).first());
    },
    async titlesForCompany(company) {
      const result = RowsResultSchema.parse(
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
      const projection: PendingNotionProjection | null = input.pendingNotionProjection
        ? {
            sourceKey: record.sourceKey,
            job: input.pendingNotionProjection.job,
            status: input.pendingNotionProjection.status,
            createdAt: input.pendingNotionProjection.createdAt,
          }
        : null;
      const statements = [
        binding.prepare(RECORD_PROCESSED_JOB_SQL).bind(...processedJobWriteValues(record)),
      ];
      if (projection) {
        statements.push(
          binding
            .prepare(RECORD_PENDING_NOTION_PROJECTION_SQL)
            .bind(
              projection.sourceKey,
              JSON.stringify(projection.job),
              projection.status,
              projection.createdAt,
            ),
        );
      }
      BatchWriteResultSchema.parse(await binding.batch(statements));
      return projection;
    },
    async listPendingNotionProjections() {
      const result = RowsResultSchema.parse(
        await binding.prepare(LIST_PENDING_NOTION_PROJECTIONS_SQL).all(),
      );
      return parsePendingNotionProjectionRows(result.results);
    },
    async markNotionProjectionComplete(sourceKey) {
      WriteResultSchema.parse(
        await binding.prepare(MARK_NOTION_PROJECTION_COMPLETE_SQL).bind(sourceKey).run(),
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
