import { afterEach, describe, expect, test } from "bun:test";
import { createJobLedger, type JobLedger } from "../../services/jobLedger";
import { recordAfterTraceFlush, recordTerminalResult } from "../recordTerminalResult";

describe("recordTerminalResult", () => {
  let ledger: JobLedger | undefined;

  afterEach(() => {
    ledger?.close();
    ledger = undefined;
  });

  test("records the terminal result before projecting it", async () => {
    ledger = createJobLedger(":memory:");
    const url = "https://jobs.example.com/role/1";

    await recordTerminalResult({
      ledger,
      url,
      job: { company: "Acme", title: "Senior Engineer" },
      outcome: "rejected",
      project: async () => {
        expect(ledger?.findByRawUrl(url)).toMatchObject({
          company: "Acme",
          title: "Senior Engineer",
          outcome: "rejected",
        });
      },
    });
  });

  test("records fuzzy duplicates without a Notion projection", async () => {
    ledger = createJobLedger(":memory:");
    const url = "https://jobs.example.com/role/2";

    await recordTerminalResult({
      ledger,
      url,
      job: { company: "Acme", title: "Staff Engineer" },
      outcome: "duplicated",
    });

    expect(ledger.findByRawUrl(url)?.outcome).toBe("duplicated");
  });

  test("records the parent trace with the terminal result", async () => {
    ledger = createJobLedger(":memory:");
    const url = "https://jobs.example.com/role/3";

    await recordTerminalResult({
      ledger,
      url,
      job: { company: "Acme", title: "Product Engineer" },
      outcome: "inserted",
      traceId: "trace-123",
    });

    expect(ledger.findByRawUrl(url)?.traceId).toBe("trace-123");
  });

  test("does not record a job when its parent trace flush fails", async () => {
    ledger = createJobLedger(":memory:");
    const currentLedger = ledger;
    const url = "https://jobs.example.com/role/4";

    await expect(
      recordAfterTraceFlush({
        flush: async () => {
          throw new Error("LangSmith unavailable");
        },
        record: () =>
          recordTerminalResult({
            ledger: currentLedger,
            url,
            job: { company: "Acme", title: "Product Engineer" },
            outcome: "inserted",
          }),
      }),
    ).rejects.toThrow("LangSmith unavailable");

    expect(ledger.findByRawUrl(url)).toBeNull();
  });
});
