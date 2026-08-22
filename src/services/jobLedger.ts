import { Database } from "bun:sqlite";

export const PROCESSED_JOB_OUTCOMES = [
  "historical",
  "inserted",
  "rejected",
  "duplicated",
  "companyApplied",
  "archived",
] as const;

export type ProcessedJobOutcome = (typeof PROCESSED_JOB_OUTCOMES)[number];

export interface ProcessedJob {
  sourceKey: string;
  rawUrl: string | null;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  firstProcessedAt: string;
  lastProcessedAt: string;
  traceId: string | null;
}

export interface CompanyExclusion {
  company: string;
  excludedAt: string;
}

export interface RecordProcessedJobInput {
  rawUrl?: string;
  sourceKey?: string;
  company: string;
  title: string;
  outcome: ProcessedJobOutcome;
  processedAt?: string;
  traceId?: string;
}

export interface ExcludeCompanyInput {
  company: string;
  excludedAt?: string;
  sourceKey?: string;
}

export interface NotionBackfillStats {
  sourceRows: number;
  urls: number;
  companyTitlePairs: number;
  urlLessRows: number;
  exclusions: number;
}

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

export interface JobLedger {
  findByRawUrl(rawUrl: string): ProcessedJob | null;
  titlesForCompany(company: string): string[];
  findCompanyExclusion(company: string): CompanyExclusion | null;
  recordProcessedJob(input: RecordProcessedJobInput): void;
  excludeCompany(input: ExcludeCompanyInput): void;
  notionBackfillStats(): NotionBackfillStats;
  markMigration(name: string, completedAt: string): void;
  hasMigration(name: string): boolean;
  close(): void;
}

export function createJobLedger(databasePath: string): JobLedger {
  const database = new Database(databasePath);
  database.run("PRAGMA journal_mode = WAL");
  database.run("PRAGMA foreign_keys = ON");
  database.run(`
    CREATE TABLE IF NOT EXISTS processed_jobs (
      source_key TEXT PRIMARY KEY,
      raw_url TEXT,
      company TEXT NOT NULL,
      normalized_company TEXT NOT NULL,
      title TEXT NOT NULL,
      normalized_title TEXT NOT NULL,
      outcome TEXT NOT NULL,
      first_processed_at TEXT NOT NULL,
      last_processed_at TEXT NOT NULL,
      trace_id TEXT
    )
  `);
  database.run(`
    CREATE INDEX IF NOT EXISTS processed_jobs_company_title
    ON processed_jobs(normalized_company, normalized_title)
  `);
  database.run(`
    CREATE INDEX IF NOT EXISTS processed_jobs_raw_url
    ON processed_jobs(raw_url)
  `);
  database.run(`
    CREATE TABLE IF NOT EXISTS company_exclusions (
      normalized_company TEXT PRIMARY KEY,
      company TEXT NOT NULL,
      excluded_at TEXT NOT NULL,
      source_key TEXT
    )
  `);
  database.run(`
    CREATE TABLE IF NOT EXISTS job_ledger_migrations (
      name TEXT PRIMARY KEY,
      completed_at TEXT NOT NULL
    )
  `);
  const notionBackfillStats = database.query<NotionBackfillStats, []>(`
    SELECT
      (SELECT COUNT(*) FROM processed_jobs WHERE source_key LIKE 'notion:%') AS sourceRows,
      (SELECT COUNT(DISTINCT raw_url) FROM processed_jobs
        WHERE source_key LIKE 'notion:%' AND raw_url IS NOT NULL) AS urls,
      (SELECT COUNT(*) FROM (
        SELECT normalized_company, normalized_title
        FROM processed_jobs
        WHERE source_key LIKE 'notion:%'
        GROUP BY normalized_company, normalized_title
      )) AS companyTitlePairs,
      (SELECT COUNT(*) FROM processed_jobs
        WHERE source_key LIKE 'notion:%' AND raw_url IS NULL) AS urlLessRows,
      (SELECT COUNT(*) FROM company_exclusions WHERE source_key LIKE 'notion:%') AS exclusions
  `);
  const markMigration = database.query<never, [string, string]>(`
    INSERT INTO job_ledger_migrations (name, completed_at)
    VALUES (?, ?)
    ON CONFLICT(name) DO UPDATE SET completed_at = excluded.completed_at
  `);
  const hasMigration = database.query<{ name: string }, [string]>(`
    SELECT name FROM job_ledger_migrations WHERE name = ?
  `);

  const findByRawUrl = database.query<ProcessedJobRow, [string]>(`
    SELECT source_key, raw_url, company, title, outcome,
      first_processed_at, last_processed_at, trace_id
    FROM processed_jobs
    WHERE raw_url = ?
  `);
  const titlesForCompany = database.query<{ title: string }, [string]>(`
    SELECT DISTINCT title
    FROM processed_jobs
    WHERE normalized_company = ?
    ORDER BY normalized_title, title
  `);
  const findCompanyExclusion = database.query<CompanyExclusionRow, [string]>(`
    SELECT company, excluded_at
    FROM company_exclusions
    WHERE normalized_company = ?
  `);
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
  >(`
    INSERT INTO processed_jobs (
      source_key,
      raw_url,
      company,
      normalized_company,
      title,
      normalized_title,
      outcome,
      first_processed_at,
      last_processed_at,
      trace_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_key) DO UPDATE SET
      raw_url = excluded.raw_url,
      company = excluded.company,
      normalized_company = excluded.normalized_company,
      title = excluded.title,
      normalized_title = excluded.normalized_title,
      outcome = excluded.outcome,
      last_processed_at = excluded.last_processed_at,
      trace_id = excluded.trace_id
  `);
  const excludeCompany = database.query<never, [string, string, string, string | null]>(`
    INSERT INTO company_exclusions (normalized_company, company, excluded_at, source_key)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(normalized_company) DO UPDATE SET
      source_key = COALESCE(excluded.source_key, company_exclusions.source_key)
  `);

  return {
    findByRawUrl(rawUrl) {
      const row = findByRawUrl.get(rawUrl);
      return row ? toProcessedJob(row) : null;
    },
    titlesForCompany(company) {
      return titlesForCompany.all(normalizeText(company)).map((row) => row.title);
    },
    findCompanyExclusion(company) {
      const row = findCompanyExclusion.get(normalizeText(company));
      return row ? { company: row.company, excludedAt: row.excluded_at } : null;
    },
    recordProcessedJob(input) {
      const processedAt = input.processedAt ?? new Date().toISOString();
      recordProcessedJob.run(
        input.sourceKey ?? sourceKeyFor(input.rawUrl),
        input.rawUrl ?? null,
        input.company,
        normalizeText(input.company),
        input.title,
        normalizeText(input.title),
        input.outcome,
        processedAt,
        processedAt,
        input.traceId ?? null,
      );
    },
    excludeCompany(input) {
      excludeCompany.run(
        normalizeText(input.company),
        input.company,
        input.excludedAt ?? new Date().toISOString(),
        input.sourceKey ?? null,
      );
    },
    notionBackfillStats() {
      const stats = notionBackfillStats.get();
      if (!stats) throw new Error("Could not read Notion backfill statistics");
      return stats;
    },
    markMigration(name, completedAt) {
      markMigration.run(name, completedAt);
    },
    hasMigration(name) {
      return hasMigration.get(name) !== null;
    },
    close() {
      database.close();
    },
  };
}

function sourceKeyFor(rawUrl: string | undefined): string {
  if (!rawUrl) {
    throw new Error("A source key is required when a processed job has no URL");
  }

  return `url:${rawUrl}`;
}

function normalizeText(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
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
