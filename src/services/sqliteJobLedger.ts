import { Database } from "bun:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import type {
  CompanyExclusion,
  JobLedger,
  NotionBackfillStats,
  ProcessedJob,
  ProcessedJobOutcome,
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

const JOB_LEDGER_SCHEMA = readFileSync(
  new URL("../../migrations/0001_job_ledger.sql", import.meta.url),
  "utf8",
);

interface ProcessedJobRow {
  source_key: string;
  raw_url: string | null;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  first_processed_at: string;
  last_processed_at: string;
  trace_id: string | null;
}

interface CompanyExclusionRow {
  company: string;
  excluded_at: string;
}

export function createSqliteJobLedger(databasePath: string): JobLedger {
  if (databasePath !== ":memory:") {
    mkdirSync(dirname(databasePath), { recursive: true });
  }
  const database = new Database(databasePath);
  database.run("PRAGMA journal_mode = WAL");
  database.run("PRAGMA foreign_keys = ON");
  database.exec(JOB_LEDGER_SCHEMA);

  const notionBackfillStats = database.query<NotionBackfillStats, []>(NOTION_BACKFILL_STATS_SQL);
  const markMigration = database.query<never, [string, string]>(MARK_MIGRATION_SQL);
  const hasMigration = database.query<{ name: string }, [string]>(HAS_MIGRATION_SQL);
  const findByRawUrl = database.query<ProcessedJobRow, [string]>(FIND_BY_RAW_URL_SQL);
  const titlesForCompany = database.query<{ title: string }, [string]>(TITLES_FOR_COMPANY_SQL);
  const findCompanyExclusion = database.query<CompanyExclusionRow, [string]>(
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
      const row = findByRawUrl.get(rawUrl);
      return row ? toProcessedJob(row) : null;
    },
    async titlesForCompany(company) {
      return titlesForCompany.all(normalizeJobLedgerText(company)).map((row) => row.title);
    },
    async findCompanyExclusion(company) {
      const row = findCompanyExclusion.get(normalizeJobLedgerText(company));
      return row ? toCompanyExclusion(row) : null;
    },
    async recordProcessedJob(input) {
      const record = createProcessedJobWriteRecord(input);
      recordProcessedJob.run(
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
      );
    },
    async excludeCompany(input) {
      const record = createCompanyExclusionWriteRecord(input);
      excludeCompany.run(
        record.normalizedCompany,
        record.company,
        record.excludedAt,
        record.sourceKey,
      );
    },
    async notionBackfillStats() {
      const stats = notionBackfillStats.get();
      if (!stats) throw new Error("Could not read Notion backfill statistics");
      return stats;
    },
    async markMigration(name, completedAt) {
      markMigration.run(name, completedAt);
    },
    async hasMigration(name) {
      return hasMigration.get(name) !== null;
    },
    async close() {
      database.close();
    },
  };
}

function toProcessedJob(row: ProcessedJobRow): ProcessedJob {
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

function toCompanyExclusion(row: CompanyExclusionRow): CompanyExclusion {
  return { company: row.company, excludedAt: row.excluded_at };
}
