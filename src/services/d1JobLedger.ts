import { z } from "zod/v4";
import type { D1DatabaseBinding } from "./d1";
import type { JobLedger, PendingNotionProjection } from "./jobLedger";
import {
  companyExclusionWriteValues,
  createCompanyExclusionWriteRecord,
  createPendingJobProjections,
  createProcessedJobWriteRecord,
  importedNotionCompanyStateWriteValues,
  normalizeJobLedgerText,
  processedJobWriteValues,
} from "./jobLedgerRecord";
import {
  parseCompanyExclusionRow,
  parseMigrationRow,
  parsePendingNotionProjectionRows,
  parsePendingReviewProjectionRows,
  parseProcessedJobRow,
  parseSelectiveNotionImportStats,
  parseTitleRows,
} from "./jobLedgerRows";
import {
  DELETE_IMPORTED_NOTION_COMPANY_STATE_SQL,
  DELETE_LEGACY_NOTION_COMPANY_EXCLUSIONS_SQL,
  DELETE_LEGACY_NOTION_PROCESSED_JOBS_SQL,
  EXCLUDE_COMPANY_SQL,
  FIND_BY_RAW_URL_SQL,
  FIND_COMPANY_EXCLUSION_SQL,
  HAS_MIGRATION_SQL,
  IMPORTED_NOTION_COMPANY_STATE_STATS_SQL,
  INSERT_IMPORTED_NOTION_COMPANY_STATE_SQL,
  LIST_PENDING_NOTION_PROJECTIONS_SQL,
  LIST_PENDING_REVIEW_PROJECTIONS_SQL,
  MARK_MIGRATION_SQL,
  MARK_NOTION_PROJECTION_COMPLETE_SQL,
  MARK_REVIEW_PROJECTION_COMPLETE_SQL,
  RECORD_PENDING_NOTION_PROJECTION_SQL,
  RECORD_PENDING_REVIEW_PROJECTION_SQL,
  RECORD_PROCESSED_JOB_SQL,
  TITLES_FOR_COMPANY_SQL,
} from "./jobLedgerSql";

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
          .bind(normalizeJobLedgerText(company), normalizeJobLedgerText(company))
          .first(),
      );
    },
    async recordProcessedJob(input) {
      const record = createProcessedJobWriteRecord(input);
      const projections = createPendingJobProjections(record.sourceKey, input.projections);
      const statements = [
        binding.prepare(RECORD_PROCESSED_JOB_SQL).bind(...processedJobWriteValues(record)),
      ];
      switch (projections.kind) {
        case "none":
          break;
        case "notion":
          statements.push(pendingNotionProjectionStatement(binding, projections.notion));
          break;
        case "notion-and-review":
          statements.push(
            pendingNotionProjectionStatement(binding, projections.notion),
            binding
              .prepare(RECORD_PENDING_REVIEW_PROJECTION_SQL)
              .bind(
                projections.review.sourceKey,
                projections.review.traceId,
                projections.review.createdAt,
              ),
          );
          break;
      }
      BatchWriteResultSchema.parse(await binding.batch(statements));
      return projections;
    },
    async nextPendingNotionProjectionBatch() {
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
    async listPendingReviewProjections() {
      const result = RowsResultSchema.parse(
        await binding.prepare(LIST_PENDING_REVIEW_PROJECTIONS_SQL).all(),
      );
      return parsePendingReviewProjectionRows(result.results);
    },
    async markReviewProjectionComplete(sourceKey) {
      WriteResultSchema.parse(
        await binding.prepare(MARK_REVIEW_PROJECTION_COMPLETE_SQL).bind(sourceKey).run(),
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
    async migrateNotionCompanyState({ states, importedAt }) {
      const statements = [
        binding.prepare(DELETE_LEGACY_NOTION_PROCESSED_JOBS_SQL),
        binding.prepare(DELETE_LEGACY_NOTION_COMPANY_EXCLUSIONS_SQL),
        binding.prepare(DELETE_IMPORTED_NOTION_COMPANY_STATE_SQL),
      ];
      for (const state of states) {
        statements.push(
          binding
            .prepare(INSERT_IMPORTED_NOTION_COMPANY_STATE_SQL)
            .bind(...importedNotionCompanyStateWriteValues(state, importedAt)),
        );
      }
      statements.push(binding.prepare(IMPORTED_NOTION_COMPANY_STATE_STATS_SQL));

      const results = z.array(z.unknown()).parse(await binding.batch(statements));
      BatchWriteResultSchema.parse(results);
      const statsResult = RowsResultSchema.parse(results.at(-1));
      const stats = parseSelectiveNotionImportStats(statsResult.results[0]);
      if (!stats) throw new Error("Could not read imported Notion company state statistics");
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

function pendingNotionProjectionStatement(
  binding: D1DatabaseBinding,
  notion: PendingNotionProjection,
) {
  return binding
    .prepare(RECORD_PENDING_NOTION_PROJECTION_SQL)
    .bind(notion.sourceKey, JSON.stringify(notion.job), notion.status, notion.createdAt);
}
