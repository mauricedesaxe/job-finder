import { Database } from "bun:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { URL as NodeUrl } from "node:url";
import type {
  JobLedger,
  PendingNotionProjection,
  ProcessedJobOutcome,
  RecordProcessedJobInput,
} from "./jobLedger";
import {
  companyExclusionWriteValues,
  createCompanyExclusionWriteRecord,
  createPendingJobProjections,
  createProcessedJobWriteRecord,
  normalizeJobLedgerText,
  processedJobWriteValues,
} from "./jobLedgerRecord";
import {
  parseCompanyExclusionRow,
  parseMigrationRow,
  parseNotionBackfillStats,
  parsePendingNotionProjectionRows,
  parsePendingReviewProjectionRows,
  parseProcessedJobRow,
  parseTitleRows,
} from "./jobLedgerRows";
import {
  EXCLUDE_COMPANY_SQL,
  FIND_BY_RAW_URL_SQL,
  FIND_COMPANY_EXCLUSION_SQL,
  HAS_MIGRATION_SQL,
  LIST_PENDING_NOTION_PROJECTIONS_SQL,
  LIST_PENDING_REVIEW_PROJECTIONS_SQL,
  MARK_MIGRATION_SQL,
  MARK_NOTION_PROJECTION_COMPLETE_SQL,
  MARK_REVIEW_PROJECTION_COMPLETE_SQL,
  NOTION_BACKFILL_STATS_SQL,
  RECORD_PENDING_NOTION_PROJECTION_SQL,
  RECORD_PENDING_REVIEW_PROJECTION_SQL,
  RECORD_PROCESSED_JOB_SQL,
  TITLES_FOR_COMPANY_SQL,
} from "./jobLedgerSql";
import { NOTION_JOB_LEDGER_BACKFILL_MIGRATION } from "./notionLedgerBackfill";

const JOB_LEDGER_SCHEMA = [
  "0001_job_ledger.sql",
  "0002_pending_notion_projections.sql",
  "0003_run_lock_and_pending_review_projections.sql",
]
  .map((name) => readFileSync(new NodeUrl(`../../migrations/${name}`, import.meta.url), "utf8"))
  .join("\n");

export function createSqliteJobLedger(
  databasePath: string,
): JobLedger & { close(): Promise<void> } {
  if (databasePath !== ":memory:") {
    mkdirSync(dirname(databasePath), { recursive: true });
  }
  const database = new Database(databasePath);
  database.run("PRAGMA journal_mode = WAL");
  database.run("PRAGMA foreign_keys = ON");
  database.exec(JOB_LEDGER_SCHEMA);

  const notionBackfillStats = database.query<Record<string, unknown>, []>(
    NOTION_BACKFILL_STATS_SQL,
  );
  const markMigration = database.query<never, [string, string]>(MARK_MIGRATION_SQL);
  const hasMigration = database.query<Record<string, unknown>, [string]>(HAS_MIGRATION_SQL);
  const findByRawUrl = database.query<Record<string, unknown>, [string]>(FIND_BY_RAW_URL_SQL);
  const titlesForCompany = database.query<Record<string, unknown>, [string]>(
    TITLES_FOR_COMPANY_SQL,
  );
  const findCompanyExclusion = database.query<Record<string, unknown>, [string]>(
    FIND_COMPANY_EXCLUSION_SQL,
  );
  const recordProcessedJob = database.query<
    never,
    [
      string,
      string | null,
      string,
      string,
      string,
      string,
      ProcessedJobOutcome,
      string,
      string,
      string | null,
    ]
  >(RECORD_PROCESSED_JOB_SQL);
  const recordPendingNotionProjection = database.query<never, [string, string, string, string]>(
    RECORD_PENDING_NOTION_PROJECTION_SQL,
  );
  const listPendingNotionProjections = database.query<Record<string, unknown>, []>(
    LIST_PENDING_NOTION_PROJECTIONS_SQL,
  );
  const markNotionProjectionComplete = database.query<never, [string]>(
    MARK_NOTION_PROJECTION_COMPLETE_SQL,
  );
  const recordPendingReviewProjection = database.query<never, [string, string, string]>(
    RECORD_PENDING_REVIEW_PROJECTION_SQL,
  );
  const listPendingReviewProjections = database.query<Record<string, unknown>, []>(
    LIST_PENDING_REVIEW_PROJECTIONS_SQL,
  );
  const markReviewProjectionComplete = database.query<never, [string]>(
    MARK_REVIEW_PROJECTION_COMPLETE_SQL,
  );
  const excludeCompany = database.query<never, [string, string, string, string | null]>(
    EXCLUDE_COMPANY_SQL,
  );

  const recordProcessedJobAtomically = database.transaction((input: RecordProcessedJobInput) => {
    const record = createProcessedJobWriteRecord(input);
    recordProcessedJob.run(...processedJobWriteValues(record));

    const projections = createPendingJobProjections(record.sourceKey, input.projections);
    switch (projections.kind) {
      case "none":
        break;
      case "notion":
        recordNotionProjection(projections.notion);
        break;
      case "notion-and-review":
        recordNotionProjection(projections.notion);
        recordPendingReviewProjection.run(
          projections.review.sourceKey,
          projections.review.traceId,
          projections.review.createdAt,
        );
        break;
    }
    return projections;
  });
  function recordNotionProjection(projection: PendingNotionProjection): void {
    recordPendingNotionProjection.run(
      projection.sourceKey,
      JSON.stringify(projection.job),
      projection.status,
      projection.createdAt,
    );
  }

  return {
    async isReadyForScrape() {
      return parseMigrationRow(hasMigration.get(NOTION_JOB_LEDGER_BACKFILL_MIGRATION));
    },
    async findByRawUrl(rawUrl) {
      return parseProcessedJobRow(findByRawUrl.get(rawUrl));
    },
    async titlesForCompany(company) {
      return parseTitleRows(titlesForCompany.all(normalizeJobLedgerText(company)));
    },
    async findCompanyExclusion(company) {
      return parseCompanyExclusionRow(findCompanyExclusion.get(normalizeJobLedgerText(company)));
    },
    async recordProcessedJob(input) {
      return recordProcessedJobAtomically(input);
    },
    async listPendingNotionProjections() {
      return parsePendingNotionProjectionRows(listPendingNotionProjections.all());
    },
    async markNotionProjectionComplete(sourceKey) {
      markNotionProjectionComplete.run(sourceKey);
    },
    async listPendingReviewProjections() {
      return parsePendingReviewProjectionRows(listPendingReviewProjections.all());
    },
    async markReviewProjectionComplete(sourceKey) {
      markReviewProjectionComplete.run(sourceKey);
    },
    async excludeCompany(input) {
      const record = createCompanyExclusionWriteRecord(input);
      excludeCompany.run(...companyExclusionWriteValues(record));
    },
    async notionBackfillStats() {
      const stats = parseNotionBackfillStats(notionBackfillStats.get());
      if (!stats) throw new Error("Could not read Notion backfill statistics");
      return stats;
    },
    async markMigration(name, completedAt) {
      markMigration.run(name, completedAt);
    },
    async hasMigration(name) {
      return parseMigrationRow(hasMigration.get(name));
    },
    async close() {
      database.close();
    },
  };
}
