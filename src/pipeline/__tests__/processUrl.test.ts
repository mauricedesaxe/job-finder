import { afterEach, describe, expect, test } from "bun:test";
import type { JobLedger } from "../../services/jobLedger";
import { createSqliteJobLedger } from "../../services/sqliteJobLedger";
import { recordTerminalResult } from "../recordTerminalResult";

describe("recordTerminalResult", () => {
  let ledger: JobLedger | undefined;

  afterEach(async () => {
    await ledger?.close();
    ledger = undefined;
  });

  test("records the terminal result before projecting it", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const url = "https://jobs.example.com/role/1";

    await recordTerminalResult({
      ledger,
      url,
      job: { company: "Acme", title: "Senior Engineer" },
      outcome: "rejected",
      traceId: "trace-123",
      project: async () => {
        expect(await ledger?.findByRawUrl(url)).toMatchObject({
          company: "Acme",
          title: "Senior Engineer",
          outcome: "rejected",
        });
      },
    });
  });

  test("records fuzzy duplicates without a Notion projection", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const url = "https://jobs.example.com/role/2";

    await recordTerminalResult({
      ledger,
      url,
      job: { company: "Acme", title: "Staff Engineer" },
      outcome: "duplicated",
      traceId: "trace-123",
    });

    expect((await ledger.findByRawUrl(url))?.outcome).toBe("duplicated");
  });

  test("records the parent trace with the terminal result", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const url = "https://jobs.example.com/role/3";

    await recordTerminalResult({
      ledger,
      url,
      job: { company: "Acme", title: "Product Engineer" },
      outcome: "inserted",
      traceId: "trace-123",
    });

    expect((await ledger.findByRawUrl(url))?.traceId).toBe("trace-123");
  });
});
