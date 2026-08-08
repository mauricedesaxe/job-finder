import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { backfillLedger, createLedger } from "../ledger";
import type { PageIdentity } from "../notion/helpers";

function withTempDb(run: (path: string) => void): void {
  const dir = mkdtempSync(join(tmpdir(), "ledger-test-"));
  try {
    run(join(dir, "ledger.sqlite"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function identity(overrides: Partial<PageIdentity> = {}): PageIdentity {
  return {
    url: "",
    company: "",
    title: "",
    status: "",
    appDate: null,
    ...overrides,
  };
}

describe("createLedger", () => {
  test("records a processed url and reports it as seen", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      ledger.record({
        url: "https://example.com/job",
        company: "Acme",
        title: "Engineer",
        outcome: "inserted",
      });
      expect(ledger.hasUrl("https://example.com/job")).toBe(true);
      expect(ledger.hasUrl("https://example.com/other")).toBe(false);
      ledger.close();
    });
  });

  test("titlesForCompany returns recorded titles for that company only", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      ledger.record({ url: "a", company: "Acme", title: "Senior Engineer", outcome: "inserted" });
      ledger.record({ url: "b", company: "Acme", title: "Staff Engineer", outcome: "inserted" });
      ledger.record({ url: "c", company: "Other", title: "Designer", outcome: "inserted" });
      expect(ledger.titlesForCompany("Acme")).toEqual(["Senior Engineer", "Staff Engineer"]);
      expect(ledger.titlesForCompany("Other")).toEqual(["Designer"]);
      ledger.close();
    });
  });

  test("re-recording an existing url is idempotent and keeps identity", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      ledger.record({ url: "a", company: "Acme", title: "Engineer", outcome: "inserted" });
      ledger.record({
        url: "a",
        company: "Acme",
        title: "Engineer",
        outcome: "archived",
        traceId: "t1",
      });
      expect(ledger.counts().urls).toBe(1);
      expect(ledger.titlesForCompany("Acme")).toEqual(["Engineer"]);
      ledger.close();
    });
  });

  test("a re-record without a trace id does not wipe a stored trace id", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      ledger.record({
        url: "a",
        company: "Acme",
        title: "Engineer",
        outcome: "inserted",
        traceId: "t1",
      });
      ledger.record({ url: "a", company: "Acme", title: "Engineer", outcome: "backfilled" });
      expect(ledger.counts().urls).toBe(1);
      expect(ledger.hasUrl("a")).toBe(true);
      expect(ledger.traceIdFor("a")).toBe("t1");
      ledger.close();
    });
  });

  test("exclude adds a whole-company suppression", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      ledger.exclude("BadCorp");
      expect(ledger.isExcluded("BadCorp")).toBe(true);
      expect(ledger.isExcluded("GoodCorp")).toBe(false);
      ledger.exclude("BadCorp");
      expect(ledger.counts().exclusions).toBe(1);
      ledger.close();
    });
  });

  test("counts reports urls, exclusions and distinct companies", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      ledger.record({ url: "a", company: "Acme", title: "Engineer", outcome: "inserted" });
      ledger.record({ url: "b", company: "Acme", title: "Staff", outcome: "rejected" });
      ledger.record({ url: "c", company: "Other", title: "Designer", outcome: "inserted" });
      ledger.exclude("BadCorp");
      expect(ledger.counts()).toEqual({ urls: 3, exclusions: 1, companies: 2 });
      ledger.close();
    });
  });

  test("persists across reopen on the same volume file", () => {
    withTempDb((path) => {
      const first = createLedger(path);
      first.record({ url: "a", company: "Acme", title: "Engineer", outcome: "inserted" });
      first.close();

      const second = createLedger(path);
      expect(second.hasUrl("a")).toBe(true);
      expect(second.titlesForCompany("Acme")).toEqual(["Engineer"]);
      second.close();
    });
  });
});

describe("backfillLedger", () => {
  test("records urls and company/title pairs from notion identities", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      const stats = backfillLedger(ledger, [
        identity({
          url: "https://e.com/a",
          company: "Acme",
          title: "Engineer",
          status: "To Review",
        }),
        identity({ url: "https://e.com/b", company: "Acme", title: "Staff", status: "To Review" }),
        identity({
          url: "https://e.com/c",
          company: "Other",
          title: "Designer",
          status: "Rejected",
        }),
      ]);
      expect(stats).toEqual({ urls: 3, exclusions: 0 });
      expect(ledger.hasUrl("https://e.com/a")).toBe(true);
      expect(ledger.titlesForCompany("Acme")).toEqual(["Engineer", "Staff"]);
      ledger.close();
    });
  });

  test("moves blocked companies into exclusions", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      backfillLedger(ledger, [
        identity({
          url: "https://e.com/x",
          company: "BadCorp",
          title: "Engineer",
          status: "Company Blocked",
        }),
        identity({
          url: "https://e.com/y",
          company: "GoodCorp",
          title: "Engineer",
          status: "To Review",
        }),
      ]);
      expect(ledger.isExcluded("BadCorp")).toBe(true);
      expect(ledger.isExcluded("GoodCorp")).toBe(false);
      expect(ledger.counts().exclusions).toBe(1);
      ledger.close();
    });
  });

  test("ignores pages without a url", () => {
    withTempDb((path) => {
      const ledger = createLedger(path);
      const stats = backfillLedger(ledger, [identity({ company: "NoUrl", title: "Engineer" })]);
      expect(stats.urls).toBe(0);
      expect(ledger.counts().urls).toBe(0);
      ledger.close();
    });
  });
});
