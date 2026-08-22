import { afterEach, describe, expect, test } from "bun:test";
import { createJobLedger, type JobLedger } from "../../services/jobLedger";
import { recordTerminalResult } from "../recordTerminalResult";

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
});
