import { afterEach, describe, expect, test } from "bun:test";
import type { JobLedger } from "../jobLedger";
import { backfillJobLedger } from "../notionLedgerBackfill";
import { createSqliteJobLedger } from "../sqliteJobLedger";

function page({
  id,
  company,
  title,
  url,
  status,
}: {
  id: string;
  company?: string;
  title?: string;
  url?: string | null;
  status?: string;
}) {
  return {
    id,
    created_time: "2026-08-01T10:00:00.000Z",
    properties: {
      Company: { type: "rich_text" as const, rich_text: company ? [{ plain_text: company }] : [] },
      "Job Title": { type: "title" as const, title: title ? [{ plain_text: title }] : [] },
      URL: { type: "url" as const, url: url ?? null },
      Status: { type: "select" as const, select: status ? { name: status } : null },
    },
  };
}

function client(pages: ReturnType<typeof page>[]) {
  return {
    databases: {
      query: async ({ start_cursor }: { start_cursor?: string }) => {
        if (!start_cursor) {
          return {
            results: pages.slice(0, 2),
            has_more: pages.length > 2,
            next_cursor: pages.length > 2 ? "next" : null,
          };
        }
        return { results: pages.slice(2), has_more: false, next_cursor: null };
      },
    },
  } as unknown as import("../notion/client").ResilientNotionClient;
}

describe("backfillJobLedger", () => {
  let ledger: JobLedger | undefined;

  afterEach(async () => {
    await ledger?.close();
    ledger = undefined;
  });

  test("imports all Notion rows and verifies an idempotent backfill", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const source = client([
      page({
        id: "page-1",
        company: "INDIGO",
        title: "INTERFACE Engineer",
        url: "https://jobs.example/1",
      }),
      page({
        id: "page-2",
        company: "indigo",
        title: "INTERFACE Engineer",
        url: null,
        status: "Company Blocked",
      }),
      page({ id: "page-3", company: "Beta", title: "Designer", url: "https://jobs.example/1" }),
      page({ id: "page-4", company: "Blocked only", status: "Company Blocked" }),
    ]);

    const first = await backfillJobLedger({
      client: source,
      databaseId: "database-id",
      ledger,
      completedAt: "2026-08-22T12:00:00.000Z",
    });
    const second = await backfillJobLedger({
      client: source,
      databaseId: "database-id",
      ledger,
      completedAt: "2026-08-22T13:00:00.000Z",
    });

    expect(first.stats).toEqual({
      sourceRows: 3,
      urls: 1,
      companyTitlePairs: 2,
      urlLessRows: 1,
      exclusions: 2,
    });
    expect(second.stats).toEqual(first.stats);
    expect((await ledger.findByRawUrl("https://jobs.example/1"))?.outcome).toBe("historical");
    expect(await ledger.titlesForCompany("INDIGO")).toEqual(["INTERFACE Engineer"]);
    expect(await ledger.findCompanyExclusion("Blocked only")).not.toBeNull();
    expect(await ledger.hasMigration("notion-job-ledger-backfill-v1")).toBe(true);
  });

  test("does not mark the migration when verification fails", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const failingLedger: JobLedger = {
      ...ledger,
      notionBackfillStats: async () => ({
        sourceRows: 0,
        urls: 0,
        companyTitlePairs: 0,
        urlLessRows: 0,
        exclusions: 0,
      }),
    };

    await expect(
      backfillJobLedger({
        client: client([page({ id: "page-1", company: "Acme", title: "Engineer" })]),
        databaseId: "database-id",
        ledger: failingLedger,
        completedAt: "2026-08-22T12:00:00.000Z",
      }),
    ).rejects.toThrow("Notion ledger backfill verification failed");

    expect(await ledger.hasMigration("notion-job-ledger-backfill-v1")).toBe(false);
  });
});
