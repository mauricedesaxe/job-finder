import { afterEach, describe, expect, test } from "bun:test";
import type { QueryDatabaseResponse } from "@notionhq/client/build/src/api-endpoints";
import type { ResilientNotionClient } from "../../services/notion";
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

  test("records the terminal result before projecting it", async () => {
    ledger = createSqliteJobLedger(":memory:");
    const notion = notionClient({
      query: async () => {
        expect(await ledger?.findByRawUrl(job.url)).toMatchObject({
          company: "Acme",
          title: "Senior Engineer",
          outcome: "rejected",
        });
        return queryResponse([]);
      },
    });

    await recordTerminalResult({
      ledger,
      job,
      outcome: "rejected",
      traceId: "trace-123",
      projection: { notion, databaseId: "database" },
    });

    expect(await ledger.listPendingNotionProjections()).toEqual([]);
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
    expect(await ledger.listPendingNotionProjections()).toEqual([]);
  });

  test("records the parent trace with the terminal result", async () => {
    ledger = createSqliteJobLedger(":memory:");

    await recordTerminalResult({
      ledger,
      job,
      outcome: "inserted",
      traceId: "trace-123",
      projection: { notion: notionClient(), databaseId: "database" },
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
      const notion = notionClient({
        create: async () => {
          throw new Error("Notion unavailable");
        },
      });

      await expect(
        recordTerminalResult({
          ledger,
          job,
          outcome,
          traceId: "trace-123",
          projection: { notion, databaseId: "database" },
        }),
      ).rejects.toThrow("Notion unavailable");
      expect(await ledger.listPendingNotionProjections()).toEqual([
        expect.objectContaining({ sourceKey: `url:${job.url}`, job, status }),
      ]);
      await ledger.close();
      ledger = undefined;
    }
  });
});

function notionClient({
  query = async () => queryResponse([]),
  create = async () => ({ object: "page" as const, id: "page-1" }),
}: {
  query?: ResilientNotionClient["databases"]["query"];
  create?: ResilientNotionClient["pages"]["create"];
} = {}): ResilientNotionClient {
  return {
    databases: {
      query,
      async retrieve() {
        throw new Error("Unexpected database retrieve");
      },
      async update() {
        throw new Error("Unexpected database update");
      },
    },
    pages: {
      create,
      async update() {
        throw new Error("Unexpected page update");
      },
    },
  };
}

function queryResponse(results: QueryDatabaseResponse["results"]): QueryDatabaseResponse {
  return {
    object: "list",
    type: "page_or_database",
    page_or_database: {},
    next_cursor: null,
    has_more: false,
    results,
  };
}
