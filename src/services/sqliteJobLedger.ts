import { Database } from "bun:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { URL } from "node:url";
import type {
  JobLedger,
  PendingNotionProjection,
  ProcessedJobOutcome,
  RecordProcessedJobInput,
} from "./jobLedger";
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

const JOB_LEDGER_SCHEMA = ["0001_job_ledger.sql", "0002_pending_notion_projections.sql"]
  .map((name) => readFileSync(new URL(`../../migrations/${name}`, import.meta.url), "utf8"))
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
  const excludeCompany = database.query<never, [string, string, string, string | null]>(
    EXCLUDE_COMPANY_SQL,
  );

  const recordProcessedJobAtomically = database.transaction((input: RecordProcessedJobInput) => {
    const record = createProcessedJobWriteRecord(input);
    recordProcessedJob.run(...processedJobWriteValues(record));
    if (!input.pendingNotionProjection) return null;

    const projection: PendingNotionProjection = {
      sourceKey: record.sourceKey,
      job: input.pendingNotionProjection.job,
      status: input.pendingNotionProjection.status,
      createdAt: input.pendingNotionProjection.createdAt,
    };
    recordPendingNotionProjection.run(
      projection.sourceKey,
      JSON.stringify(projection.job),
      projection.status,
      projection.createdAt,
    );
    return projection;
  });

  return {
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
