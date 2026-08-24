import { Database } from "bun:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import type { JobLedger, ProcessedJobOutcome } from "./jobLedger";
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

const JOB_LEDGER_SCHEMA = readFileSync(
  new URL("../../migrations/0001_job_ledger.sql", import.meta.url),
  "utf8",
);

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
  const excludeCompany = database.query<never, [string, string, string, string | null]>(
    EXCLUDE_COMPANY_SQL,
  );

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
      const record = createProcessedJobWriteRecord(input);
      recordProcessedJob.run(...processedJobWriteValues(record));
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
