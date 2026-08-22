import { afterEach, describe, expect, test } from "bun:test";
import { createJobLedger, type JobLedger } from "../jobLedger.ts";

describe("job ledger", () => {
  let ledger: JobLedger | undefined;

  afterEach(() => {
    ledger?.close();
    ledger = undefined;
  });

  test("finds a processed job by its exact raw URL", () => {
    ledger = createJobLedger(":memory:");
    ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/role?id=1",
      company: "Acme",
      title: "Senior Engineer",
      outcome: "inserted",
      processedAt: "2026-08-22T10:00:00.000Z",
      traceId: "trace-1",
    });

    expect(ledger.findByRawUrl("https://jobs.example.com/role?id=1")).toEqual({
      sourceKey: "url:https://jobs.example.com/role?id=1",
      rawUrl: "https://jobs.example.com/role?id=1",
      company: "Acme",
      title: "Senior Engineer",
      outcome: "inserted",
      firstProcessedAt: "2026-08-22T10:00:00.000Z",
      lastProcessedAt: "2026-08-22T10:00:00.000Z",
      traceId: "trace-1",
    });
    expect(ledger.findByRawUrl("https://jobs.example.com/role?id=01")).toBeNull();
  });

  test("returns titles by normalized company", () => {
    ledger = createJobLedger(":memory:");
    ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/1",
      company: "  ACME   Labs ",
      title: "Senior Engineer",
      outcome: "inserted",
    });
    ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/2",
      company: "Acme Labs",
      title: "Product Engineer",
      outcome: "rejected",
    });

    expect(ledger.titlesForCompany("acme labs")).toEqual(["Product Engineer", "Senior Engineer"]);
  });

  test("updates an existing source key instead of creating another record", () => {
    ledger = createJobLedger(":memory:");
    ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/1",
      company: "Acme",
      title: "Engineer",
      outcome: "rejected",
      processedAt: "2026-08-22T10:00:00.000Z",
    });
    ledger.recordProcessedJob({
      rawUrl: "https://jobs.example.com/1",
      company: "Acme",
      title: "Senior Engineer",
      outcome: "inserted",
      processedAt: "2026-08-22T11:00:00.000Z",
      traceId: "trace-2",
    });

    expect(ledger.findByRawUrl("https://jobs.example.com/1")).toMatchObject({
      title: "Senior Engineer",
      outcome: "inserted",
      firstProcessedAt: "2026-08-22T10:00:00.000Z",
      lastProcessedAt: "2026-08-22T11:00:00.000Z",
      traceId: "trace-2",
    });
    expect(ledger.titlesForCompany("Acme")).toEqual(["Senior Engineer"]);
  });

  test("keeps URL-less records under their source key", () => {
    ledger = createJobLedger(":memory:");
    ledger.recordProcessedJob({
      sourceKey: "notion:page-123",
      company: "Acme",
      title: "Legacy Engineer",
      outcome: "archived",
    });

    expect(ledger.findByRawUrl("https://jobs.example.com/missing")).toBeNull();
    expect(ledger.titlesForCompany("Acme")).toEqual(["Legacy Engineer"]);
  });

  test("records company exclusions once and finds them by normalized company", () => {
    ledger = createJobLedger(":memory:");
    ledger.excludeCompany({
      company: "  Acme   Labs ",
      excludedAt: "2026-08-22T10:00:00.000Z",
    });
    ledger.excludeCompany({
      company: "acme labs",
      excludedAt: "2026-08-22T11:00:00.000Z",
    });

    expect(ledger.findCompanyExclusion("ACME LABS")).toEqual({
      company: "  Acme   Labs ",
      excludedAt: "2026-08-22T10:00:00.000Z",
    });
  });
});
