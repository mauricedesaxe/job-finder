import { expect, test } from "bun:test";
import type { JobLedger } from "../jobLedger";
import { createSqliteJobLedger } from "../sqliteJobLedger";
import {
  JOB_LEDGER_CONFORMANCE_RESULT,
  runJobLedgerConformanceScenario,
} from "./jobLedgerConformance";

test("preserves the job ledger contract in SQLite", async () => {
  const ledger: JobLedger = createSqliteJobLedger(":memory:");
  try {
    expect(JOB_LEDGER_CONFORMANCE_RESULT).toEqual(await runJobLedgerConformanceScenario(ledger));
  } finally {
    await ledger.close();
  }
});
