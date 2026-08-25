import { Database } from "bun:sqlite";
import { expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod/v4";
import type { PendingNotionProjection } from "../jobLedger";
import { NOTION_JOB_LEDGER_BACKFILL_MIGRATION } from "../notionLedgerBackfill";
import { createSqliteJobLedger } from "../sqliteJobLedger";
import {
  JOB_LEDGER_CONFORMANCE_RESULT,
  runJobLedgerConformanceScenario,
} from "./jobLedgerConformance";

test("preserves the job ledger contract in SQLite", async () => {
  const ledger = createSqliteJobLedger(":memory:");
  try {
    expect(JOB_LEDGER_CONFORMANCE_RESULT).toEqual(await runJobLedgerConformanceScenario(ledger));
  } finally {
    await ledger.close();
  }
});

test("requires the Notion backfill before SQLite scraping", async () => {
  const ledger = createSqliteJobLedger(":memory:");
  try {
    expect(await ledger.isReadyForScrape()).toBe(false);
    await ledger.markMigration(NOTION_JOB_LEDGER_BACKFILL_MIGRATION, "2026-08-25T00:00:00.000Z");
    expect(await ledger.isReadyForScrape()).toBe(true);
  } finally {
    await ledger.close();
  }
});

test("rejects malformed stored outcomes in SQLite", async () => {
  const directory = await mkdtemp(join(tmpdir(), "job-ledger-sqlite-"));
  const databasePath = join(directory, "ledger.sqlite");
  const ledger = createSqliteJobLedger(databasePath);
  await ledger.close();

  const database = new Database(databasePath);
  database.run(`
    INSERT INTO processed_jobs (
      source_key, raw_url, company, normalized_company, title, normalized_title,
      outcome, first_processed_at, last_processed_at, trace_id
    ) VALUES (
      'source:malformed', 'https://jobs.example.com/malformed', 'Acme', 'acme',
      'Engineer', 'engineer', 'not-an-outcome',
      '2026-08-22T10:00:00.000Z', '2026-08-22T10:00:00.000Z', NULL
    )
  `);
  database.close();

  const reopenedLedger = createSqliteJobLedger(databasePath);
  try {
    await expect(
      reopenedLedger.findByRawUrl("https://jobs.example.com/malformed"),
    ).rejects.toBeInstanceOf(z.ZodError);
  } finally {
    await reopenedLedger.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("rejects malformed stored projections in SQLite", async () => {
  const directory = await mkdtemp(join(tmpdir(), "job-ledger-projection-"));
  const databasePath = join(directory, "ledger.sqlite");
  const ledger = createSqliteJobLedger(databasePath);
  await ledger.recordProcessedJob({
    sourceKey: "source:malformed-projection",
    rawUrl: "https://jobs.example.com/malformed-projection",
    company: "Acme",
    title: "Engineer",
    outcome: "inserted",
  });
  await ledger.close();

  const database = new Database(databasePath);
  database.run(
    `INSERT INTO pending_notion_projections (source_key, job_json, status, created_at)
     VALUES (?, ?, ?, ?)`,
    [
      "source:malformed-projection",
      JSON.stringify({ title: "Incomplete" }),
      "To Review",
      "2026-08-24T10:00:00.000Z",
    ],
  );
  database.close();

  const reopenedLedger = createSqliteJobLedger(databasePath);
  try {
    await expect(reopenedLedger.listPendingNotionProjections()).rejects.toBeInstanceOf(z.ZodError);
  } finally {
    await reopenedLedger.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("rolls back the processed job when its projection cannot be stored", async () => {
  const directory = await mkdtemp(join(tmpdir(), "job-ledger-atomic-"));
  const databasePath = join(directory, "ledger.sqlite");
  let ledger = createSqliteJobLedger(databasePath);
  await ledger.close();

  const database = new Database(databasePath);
  database.run(`
    CREATE TRIGGER reject_pending_projection
    BEFORE INSERT ON pending_notion_projections
    BEGIN
      SELECT RAISE(ABORT, 'projection write failed');
    END
  `);
  database.close();

  ledger = createSqliteJobLedger(databasePath);
  const pendingNotionProjection = pendingProjection("source:atomic");
  try {
    await expect(
      ledger.recordProcessedJob({
        sourceKey: pendingNotionProjection.sourceKey,
        rawUrl: pendingNotionProjection.job.url,
        company: pendingNotionProjection.job.company,
        title: pendingNotionProjection.job.title,
        outcome: "inserted",
        projections: {
          kind: "notion",
          notion: {
            job: pendingNotionProjection.job,
            status: pendingNotionProjection.status,
            createdAt: pendingNotionProjection.createdAt,
          },
        },
      }),
    ).rejects.toThrow("projection write failed");
    expect(await ledger.findByRawUrl(pendingNotionProjection.job.url)).toBeNull();
    expect(await ledger.listPendingNotionProjections()).toEqual([]);
    expect(await ledger.listPendingReviewProjections()).toEqual([]);
  } finally {
    await ledger.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("rolls back terminal state and both outboxes when review storage fails", async () => {
  const directory = await mkdtemp(join(tmpdir(), "job-ledger-review-atomic-"));
  const databasePath = join(directory, "ledger.sqlite");
  let localLedger = createSqliteJobLedger(databasePath);
  await localLedger.close();

  const database = new Database(databasePath);
  database.run(`
    CREATE TRIGGER reject_pending_review_projection
    BEFORE INSERT ON pending_review_projections
    BEGIN
      SELECT RAISE(ABORT, 'review projection write failed');
    END
  `);
  database.close();

  localLedger = createSqliteJobLedger(databasePath);
  const pendingNotionProjection = pendingProjection("source:review-atomic");
  try {
    await expect(
      localLedger.recordProcessedJob({
        sourceKey: pendingNotionProjection.sourceKey,
        rawUrl: pendingNotionProjection.job.url,
        company: pendingNotionProjection.job.company,
        title: pendingNotionProjection.job.title,
        outcome: "inserted",
        projections: {
          kind: "notion-and-review",
          notion: {
            job: pendingNotionProjection.job,
            status: pendingNotionProjection.status,
            createdAt: pendingNotionProjection.createdAt,
          },
          review: {
            traceId: "trace-review-atomic",
            createdAt: pendingNotionProjection.createdAt,
          },
        },
      }),
    ).rejects.toThrow("review projection write failed");
    expect(await localLedger.findByRawUrl(pendingNotionProjection.job.url)).toBeNull();
    expect(await localLedger.listPendingNotionProjections()).toEqual([]);
    expect(await localLedger.listPendingReviewProjections()).toEqual([]);
  } finally {
    await localLedger.close();
    await rm(directory, { recursive: true, force: true });
  }
});

function pendingProjection(sourceKey: string): PendingNotionProjection {
  return {
    sourceKey,
    job: {
      title: "Atomic Engineer",
      company: "Atomic Co",
      url: "https://jobs.example.com/atomic",
      source: "Example",
      keywordsMatched: ["engineer"],
      datePosted: null,
      dateScraped: "2026-08-24",
      description: "Atomic projection",
      location: "Remote",
      profile: "Backend",
    },
    status: "To Review",
    createdAt: "2026-08-24T10:00:00.000Z",
  };
}
