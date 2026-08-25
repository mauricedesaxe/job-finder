import { Database } from "bun:sqlite";
import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { JobLedger } from "../jobLedger";
import type { ResilientNotionClient } from "../notion/client";
import {
  initializeJobLedgerFromNotion,
  isJobLedgerReadyForScrape,
  NOTION_COMPANY_STATE_IMPORT_MIGRATION,
} from "../notionLedgerInitialization";
import { createSqliteJobLedger } from "../sqliteJobLedger";

interface QueryCall {
  start_cursor?: string;
  filter?: unknown;
}

function page({
  id,
  company,
  status,
  applicationDate,
}: {
  id: string;
  company?: string;
  status?: string;
  applicationDate?: string;
}) {
  return {
    id,
    properties: {
      Company: { type: "rich_text" as const, rich_text: company ? [{ plain_text: company }] : [] },
      Status: { type: "select" as const, select: status ? { name: status } : null },
      "Application Date": {
        type: "date" as const,
        date: applicationDate ? { start: applicationDate, end: null, time_zone: null } : null,
      },
      "Job Title": { type: "title" as const, title: [{ plain_text: "Must not import" }] },
      URL: { type: "url" as const, url: `https://jobs.example/${id}` },
    },
  };
}

function client(pages: ReturnType<typeof page>[], queryCalls: QueryCall[] = []) {
  return {
    client: {
      databases: {
        query: async (query: QueryCall) => {
          queryCalls.push(query);
          if (!query.start_cursor) {
            return {
              results: pages.slice(0, 4),
              has_more: pages.length > 4,
              next_cursor: pages.length > 4 ? "next" : null,
            };
          }
          return { results: pages.slice(4), has_more: false, next_cursor: null };
        },
      },
    } as unknown as ResilientNotionClient,
    queryCalls,
  };
}

describe("initializeJobLedgerFromNotion", () => {
  let ledger: ReturnType<typeof createSqliteJobLedger> | undefined;
  const directories: string[] = [];

  afterEach(async () => {
    await ledger?.close();
    ledger = undefined;
    await Promise.all(
      directories.splice(0).map((path) => rm(path, { recursive: true, force: true })),
    );
  });

  test("imports only blocked companies and recent application state", async () => {
    const directory = await mkdtemp(join(tmpdir(), "notion-company-state-"));
    directories.push(directory);
    const databasePath = join(directory, "ledger.sqlite");
    ledger = createSqliteJobLedger(databasePath);
    await ledger.recordProcessedJob({
      sourceKey: "notion:legacy-job",
      rawUrl: "https://jobs.example/legacy",
      company: "Legacy Job Co",
      title: "Legacy Engineer",
      outcome: "historical",
      processedAt: "2026-01-01T00:00:00.000Z",
    });
    await ledger.excludeCompany({
      company: "Legacy Notion Block",
      excludedAt: "2026-01-01T00:00:00.000Z",
      sourceKey: "notion:legacy-block",
    });
    await ledger.excludeCompany({
      company: "Runtime Block",
      excludedAt: "2026-01-01T00:00:00.000Z",
    });
    await ledger.excludeCompany({
      company: "Review Block",
      excludedAt: "2026-01-02T00:00:00.000Z",
      sourceKey: "langsmith-review:review-1",
    });
    const source = client([
      page({ id: "blocked", company: "Blocked Co", status: "Company Blocked" }),
      page({ id: "recent", company: "Recent Co", applicationDate: "2026-03-01" }),
      page({ id: "cutoff", company: "Cutoff Co", applicationDate: "2026-02-25" }),
      page({ id: "stale", company: "Stale Co", applicationDate: "2026-02-24" }),
      page({ id: "ordinary", company: "Ordinary Co", status: "Rejected" }),
      page({ id: "dupe-old", company: " Dupe   Co ", applicationDate: "2026-03-02" }),
      page({ id: "dupe-new", company: "dupe co", applicationDate: "2026-04-05" }),
      page({
        id: "both",
        company: "Both Co",
        status: "Company Blocked",
        applicationDate: "2026-05-01",
      }),
    ]);

    const first = await initializeJobLedgerFromNotion({
      client: source.client,
      databaseId: "database-id",
      ledger,
      completedAt: "2026-08-25T12:00:00.000Z",
    });
    const second = await initializeJobLedgerFromNotion({
      client: source.client,
      databaseId: "database-id",
      ledger,
      completedAt: "2026-08-25T13:00:00.000Z",
    });

    expect(first).toEqual({
      kind: "initialized",
      stats: { blockedCompanies: 2, recentApplications: 4 },
    });
    expect(second).toEqual({ kind: "already-initialized" });
    expect(source.queryCalls).toHaveLength(2);
    expect(source.queryCalls[0]?.filter).toEqual({
      or: [
        { property: "Status", select: { equals: "Company Blocked" } },
        { property: "Application Date", date: { on_or_after: "2026-02-25" } },
      ],
    });
    expect(await ledger.findCompanyExclusion("blocked co")).not.toBeNull();
    expect(await ledger.findCompanyExclusion("both co")).not.toBeNull();
    expect(await ledger.findCompanyExclusion("recent co")).toBeNull();
    expect(await ledger.findCompanyExclusion("legacy notion block")).toBeNull();
    expect(await ledger.findCompanyExclusion("runtime block")).toEqual({
      company: "Runtime Block",
      excludedAt: "2026-01-01T00:00:00.000Z",
    });
    expect(await ledger.findCompanyExclusion("review block")).toEqual({
      company: "Review Block",
      excludedAt: "2026-01-02T00:00:00.000Z",
    });

    await ledger.close();
    ledger = undefined;
    const database = new Database(databasePath, { readonly: true });
    const rows = database
      .query(
        "SELECT normalized_company, kind, imported_at, application_date FROM imported_notion_company_state ORDER BY normalized_company, kind",
      )
      .all();
    const processedJobs = database.query("SELECT COUNT(*) AS count FROM processed_jobs").get();
    const exclusions = database
      .query("SELECT company, source_key FROM company_exclusions ORDER BY company")
      .all();
    database.close();

    expect(rows).toEqual([
      {
        normalized_company: "blocked co",
        kind: "blocked",
        imported_at: "2026-08-25T12:00:00.000Z",
        application_date: null,
      },
      {
        normalized_company: "both co",
        kind: "blocked",
        imported_at: "2026-08-25T12:00:00.000Z",
        application_date: null,
      },
      {
        normalized_company: "both co",
        kind: "recent-application",
        imported_at: "2026-08-25T12:00:00.000Z",
        application_date: "2026-05-01",
      },
      {
        normalized_company: "cutoff co",
        kind: "recent-application",
        imported_at: "2026-08-25T12:00:00.000Z",
        application_date: "2026-02-25",
      },
      {
        normalized_company: "dupe co",
        kind: "recent-application",
        imported_at: "2026-08-25T12:00:00.000Z",
        application_date: "2026-04-05",
      },
      {
        normalized_company: "recent co",
        kind: "recent-application",
        imported_at: "2026-08-25T12:00:00.000Z",
        application_date: "2026-03-01",
      },
    ]);
    expect(processedJobs).toEqual({ count: 0 });
    expect(exclusions).toEqual([
      { company: "Review Block", source_key: "langsmith-review:review-1" },
      { company: "Runtime Block", source_key: null },
    ]);
  });

  test("does not accept the old marker as readiness", async () => {
    ledger = createSqliteJobLedger(":memory:");
    await ledger.markMigration("notion-job-ledger-backfill-v1", "2026-08-25T00:00:00.000Z");
    expect(await isJobLedgerReadyForScrape(ledger)).toBe(false);

    const source = client([]);
    await initializeJobLedgerFromNotion({
      client: source.client,
      databaseId: "database-id",
      ledger,
      completedAt: "2026-08-25T12:00:00.000Z",
    });
    expect(source.queryCalls).toHaveLength(1);
    expect(await isJobLedgerReadyForScrape(ledger)).toBe(true);
  });

  test("does not mark initialization when persisted counts differ", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const failingLedger: JobLedger = {
      ...ledger,
      migrateNotionCompanyState: async () => ({
        blockedCompanies: 0,
        recentApplications: 0,
      }),
    };

    await expect(
      initializeJobLedgerFromNotion({
        client: client([page({ id: "blocked", company: "Blocked Co", status: "Company Blocked" })])
          .client,
        databaseId: "database-id",
        ledger: failingLedger,
        completedAt: "2026-08-25T12:00:00.000Z",
      }),
    ).rejects.toThrow("Notion company state initialization verification failed");

    expect(await ledger.hasMigration(NOTION_COMPANY_STATE_IMPORT_MIGRATION)).toBe(false);
  });
});
