import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { QueryDatabaseResponse } from "@notionhq/client/build/src/api-endpoints";
import type { PendingNotionProjection } from "../../services/jobLedger";
import type { ResilientNotionClient } from "../../services/notion";
import { createSqliteJobLedger } from "../../services/sqliteJobLedger";
import type { JobListing } from "../../types";
import { replayPendingNotionProjections } from "../notionProjection";

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

const projection = {
  sourceKey: `url:${job.url}`,
  job,
  status: "To Review",
  createdAt: "2026-08-24T10:00:00.000Z",
} satisfies PendingNotionProjection;

describe("replayPendingNotionProjections", () => {
  let ledger: ReturnType<typeof createSqliteJobLedger> | undefined;

  afterEach(async () => {
    await ledger?.close();
    ledger = undefined;
  });

  test("projects durable work left before Notion creation", async () => {
    const directory = await mkdtemp(join(tmpdir(), "pending-notion-projection-"));
    const databasePath = join(directory, "ledger.sqlite");
    try {
      ledger = createSqliteJobLedger(databasePath);
      await seedProjection(ledger);
      await ledger.close();
      ledger = createSqliteJobLedger(databasePath);

      let creates = 0;
      const notion = notionClient({
        query: async (args) => {
          expect(args.filter).toEqual({ property: "URL", url: { equals: job.url } });
          return queryResponse([]);
        },
        create: async () => {
          creates++;
          return { object: "page", id: "page-1" };
        },
      });

      await replayPendingNotionProjections({ ledger, notion, databaseId: "database" });

      expect(creates).toBe(1);
      expect(await ledger.listPendingNotionProjections()).toEqual([]);
    } finally {
      await ledger?.close();
      ledger = undefined;
      await rm(directory, { recursive: true, force: true });
    }
  });

  test("uses the exact URL to prevent a duplicate after response loss", async () => {
    ledger = createSqliteJobLedger(":memory:");
    await seedProjection(ledger);
    let pageExists = false;
    let creates = 0;
    let queries = 0;
    const notion = notionClient({
      query: async () => {
        queries++;
        return queryResponse(pageExists ? [{ object: "page", id: "page-1" }] : []);
      },
      create: async () => {
        creates++;
        pageExists = true;
        throw new Error("response lost");
      },
    });

    await expect(
      replayPendingNotionProjections({ ledger, notion, databaseId: "database" }),
    ).rejects.toThrow("Could not replay all pending Notion projections");
    expect(await ledger.listPendingNotionProjections()).toHaveLength(1);

    await replayPendingNotionProjections({ ledger, notion, databaseId: "database" });
    await replayPendingNotionProjections({ ledger, notion, databaseId: "database" });

    expect(creates).toBe(1);
    expect(queries).toBe(2);
    expect(await ledger.listPendingNotionProjections()).toEqual([]);
  });

  test("keeps pending state when the exact URL query fails", async () => {
    ledger = createSqliteJobLedger(":memory:");
    await seedProjection(ledger);
    const notion = notionClient({
      query: async () => {
        throw new Error("query failed");
      },
    });

    await expect(
      replayPendingNotionProjections({ ledger, notion, databaseId: "database" }),
    ).rejects.toThrow("Could not replay all pending Notion projections");

    expect(await ledger.listPendingNotionProjections()).toEqual([projection]);
  });

  test("drains Notion projections in bounded pages", async () => {
    ledger = createSqliteJobLedger(":memory:");
    for (let index = 0; index < 11; index++) {
      await seedProjection(ledger, {
        ...job,
        title: `Senior Engineer ${index}`,
        url: `https://jobs.example.com/role/${index}`,
      });
    }
    const listPending = spyOn(ledger, "listPendingNotionProjections");
    let creates = 0;
    const notion = notionClient({
      create: async () => {
        creates++;
        return { object: "page", id: `page-${creates}` };
      },
    });

    await replayPendingNotionProjections({ ledger, notion, databaseId: "database" });

    expect(creates).toBe(11);
    expect(listPending.mock.calls).toEqual([[], [], []]);
    expect(await ledger.listPendingNotionProjections()).toEqual([]);
  });
});

async function seedProjection(
  ledger: ReturnType<typeof createSqliteJobLedger>,
  queuedJob: JobListing = job,
): Promise<void> {
  await ledger.recordProcessedJob({
    rawUrl: queuedJob.url,
    company: queuedJob.company,
    title: queuedJob.title,
    outcome: "inserted",
    processedAt: projection.createdAt,
    projections: {
      kind: "notion",
      notion: {
        job: queuedJob,
        status: projection.status,
        createdAt: projection.createdAt,
      },
    },
  });
}

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
