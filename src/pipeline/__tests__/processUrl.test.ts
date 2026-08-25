import { afterEach, describe, expect, test } from "bun:test";
import { createSqliteJobLedger } from "../../services/sqliteJobLedger";
import type { JobListing, JobStatus } from "../../types";
import { recordTerminalResult, type TerminalProcessedJobOutcome } from "../recordTerminalResult";

const job = {
  title: "Senior Engineer",
  company: "Acme",
  url: "https://jobs.example.com/role/1",
  source: "Example",
  keywordsMatched: ["engineer"],
  datePosted: "2026-08-23",
  dateScraped: "2026-08-24",
  description: "Build reliable systems.",
  location: "Remote",
  profile: "Backend",
} satisfies JobListing;

describe("recordTerminalResult", () => {
  let ledger: ReturnType<typeof createSqliteJobLedger> | undefined;

  afterEach(async () => {
    await ledger?.close();
    ledger = undefined;
  });

  test("records the terminal result and defers its Notion projection", async () => {
    ledger = createSqliteJobLedger(":memory:");

    await recordTerminalResult({
      ledger,
      job,
      outcome: "rejected",
      traceId: "trace-123",
    });

    expect(await ledger.findByRawUrl(job.url)).toMatchObject({
      company: "Acme",
      title: "Senior Engineer",
      outcome: "rejected",
    });
    expect(await ledger.nextPendingNotionProjectionBatch()).toEqual([
      expect.objectContaining({ job, status: "Auto-Rejected" }),
    ]);
  });

  test("records fuzzy duplicates without a Notion projection", async () => {
    ledger = createSqliteJobLedger(":memory:");

    await recordTerminalResult({
      ledger,
      job,
      outcome: "duplicated",
      traceId: "trace-123",
    });

    expect((await ledger.findByRawUrl(job.url))?.outcome).toBe("duplicated");
    expect(await ledger.nextPendingNotionProjectionBatch()).toEqual([]);
  });

  test("records the parent trace with the terminal result", async () => {
    ledger = createSqliteJobLedger(":memory:");

    await recordTerminalResult({
      ledger,
      job,
      outcome: "inserted",
      traceId: "trace-123",
      review: { enqueue: async () => undefined },
    });

    expect((await ledger.findByRawUrl(job.url))?.traceId).toBe("trace-123");
  });

  test("maps every projected terminal outcome to its Notion status", async () => {
    const mappings = [
      ["inserted", "To Review"],
      ["rejected", "Auto-Rejected"],
      ["companyApplied", "Company Applied"],
      ["archived", "Archived"],
    ] as const satisfies readonly (readonly [
      Exclude<TerminalProcessedJobOutcome, "duplicated">,
      JobStatus,
    ])[];

    for (const [outcome, status] of mappings) {
      ledger = createSqliteJobLedger(":memory:");
      await (outcome === "inserted"
        ? recordTerminalResult({
            ledger,
            job,
            outcome,
            traceId: "trace-123",
            review: { enqueue: async () => undefined },
          })
        : recordTerminalResult({
            ledger,
            job,
            outcome,
            traceId: "trace-123",
          }));
      expect(await ledger.nextPendingNotionProjectionBatch()).toEqual([
        expect.objectContaining({ sourceKey: `url:${job.url}`, job, status }),
      ]);
      await ledger.close();
      ledger = undefined;
    }
  });
});
