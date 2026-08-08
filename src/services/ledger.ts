import { Database } from "bun:sqlite";
import type { PageIdentity } from "./notion/helpers";

/** A processed job, keyed by its canonical URL. */
export interface LedgerEntry {
  url: string;
  company: string;
  title: string;
  outcome: string;
  traceId?: string;
}

export interface BackfillStats {
  urls: number;
  exclusions: number;
}

export interface ProcessLedger {
  hasUrl(url: string): boolean;
  titlesForCompany(company: string): string[];
  isExcluded(company: string): boolean;
  traceIdFor(url: string): string | null;
  record(entry: LedgerEntry): void;
  exclude(company: string): void;
  counts(): { urls: number; exclusions: number; companies: number };
  close(): void;
}

/**
 * The durable processed-job store behind deduplication. Holds every canonical
 * URL the pipeline has seen, the company/title identity used for fuzzy dedup,
 * and an explicit whole-company suppression list. Notion keeps CRM state and
 * recent-application suppression; this file owns machine state.
 */
export function createLedger(path: string): ProcessLedger {
  const db = new Database(path);
  db.exec("PRAGMA journal_mode = WAL;");
  db.exec("PRAGMA busy_timeout = 5000;");
  db.exec("PRAGMA synchronous = NORMAL;");

  db.exec(`
    CREATE TABLE IF NOT EXISTS processed_jobs (
      canonical_url TEXT PRIMARY KEY,
      company TEXT NOT NULL DEFAULT '',
      title TEXT NOT NULL DEFAULT '',
      outcome TEXT NOT NULL,
      processed_at TEXT NOT NULL,
      trace_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_processed_jobs_company ON processed_jobs(company);
    CREATE TABLE IF NOT EXISTS company_exclusions (
      company TEXT PRIMARY KEY
    );
  `);

  const insert = db.query(`
    INSERT INTO processed_jobs (canonical_url, company, title, outcome, processed_at, trace_id)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(canonical_url) DO UPDATE SET
      outcome = excluded.outcome,
      processed_at = excluded.processed_at,
      trace_id = COALESCE(excluded.trace_id, processed_jobs.trace_id)
  `);
  const hasUrl = db.query("SELECT 1 FROM processed_jobs WHERE canonical_url = ? LIMIT 1");
  const titles = db.query("SELECT title FROM processed_jobs WHERE company = ?");
  const isExcluded = db.query("SELECT 1 FROM company_exclusions WHERE company = ? LIMIT 1");
  const traceIdFor = db.query(
    "SELECT trace_id FROM processed_jobs WHERE canonical_url = ? LIMIT 1",
  );
  const exclude = db.query("INSERT OR IGNORE INTO company_exclusions (company) VALUES (?)");
  const counts = db.query(`
    SELECT
      (SELECT count(*) FROM processed_jobs) AS urls,
      (SELECT count(*) FROM company_exclusions) AS exclusions,
      (SELECT count(DISTINCT company) FROM processed_jobs) AS companies
  `);

  return {
    hasUrl(url) {
      return hasUrl.get(url) !== null;
    },
    titlesForCompany(company) {
      return (titles.all(company) as Array<{ title: string }>)
        .map((r) => r.title)
        .filter((t) => t !== "");
    },
    isExcluded(company) {
      return isExcluded.get(company) !== null;
    },
    traceIdFor(url) {
      const row = traceIdFor.get(url) as { trace_id: string | null } | undefined;
      return row?.trace_id ?? null;
    },
    record(entry) {
      insert.run(
        entry.url,
        entry.company.trim(),
        entry.title.trim(),
        entry.outcome,
        new Date().toISOString(),
        entry.traceId ?? null,
      );
    },
    exclude(company) {
      exclude.run(company.trim());
    },
    counts() {
      return counts.get() as { urls: number; exclusions: number; companies: number };
    },
    close() {
      db.close();
    },
  };
}

/**
 * Populate the ledger from Notion's existing machine state so dedup keeps
 * working once it reads the ledger instead of Notion. Idempotent per URL.
 * Blocked companies become explicit exclusions.
 */
export function backfillLedger(ledger: ProcessLedger, identities: PageIdentity[]): BackfillStats {
  const urls = new Set<string>();
  const exclusions = new Set<string>();

  for (const id of identities) {
    if (id.url) {
      ledger.record({ url: id.url, company: id.company, title: id.title, outcome: "backfilled" });
      urls.add(id.url);
    }
    if (id.company && id.status === "Company Blocked") {
      ledger.exclude(id.company);
      exclusions.add(id.company);
    }
  }

  return { urls: urls.size, exclusions: exclusions.size };
}
