import { Database } from "bun:sqlite";
import { expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod/v4";
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
