import { describe, expect, test } from "bun:test";
import type { ResilientNotionClient } from "../../services/notion";
import { prune, pruneDecision } from "../prune";

// Cutoff = 6 months before 2026-05-27.
const cutoff = new Date("2025-11-27T00:00:00.000Z");

describe("pruneDecision", () => {
  test("prunes an old To Review job past the window", () => {
    expect(
      pruneDecision(
        { status: "To Review", applicationDate: null, dateScraped: "2025-01-01" },
        cutoff,
      ),
    ).toBe("prune");
  });

  test("prunes an old Auto-Rejected job", () => {
    expect(
      pruneDecision(
        { status: "Auto-Rejected", applicationDate: null, dateScraped: "2024-06-01" },
        cutoff,
      ),
    ).toBe("prune");
  });

  test("never prunes a Company Blocked job, however old", () => {
    expect(
      pruneDecision(
        { status: "Company Blocked", applicationDate: null, dateScraped: "2020-01-01" },
        cutoff,
      ),
    ).toBe("keep-blocked");
  });

  test("keeps a job whose application is still inside the window", () => {
    expect(
      pruneDecision(
        { status: "Applied", applicationDate: "2026-03-01", dateScraped: "2024-01-01" },
        cutoff,
      ),
    ).toBe("keep-locked");
  });

  test("prunes an application that has aged past the window", () => {
    expect(
      pruneDecision(
        { status: "Applied", applicationDate: "2025-01-01", dateScraped: "2025-01-01" },
        cutoff,
      ),
    ).toBe("prune");
  });

  test("keeps a job with no Date Scraped", () => {
    expect(
      pruneDecision({ status: "To Review", applicationDate: null, dateScraped: null }, cutoff),
    ).toBe("keep-no-date");
  });
});

const TODAY = new Date().toISOString().slice(0, 10);

function makePage(opts: {
  id: string;
  status: string;
  appDate?: string | null;
  scraped?: string | null;
}) {
  const scraped = opts.scraped === undefined ? "2020-01-01" : opts.scraped;
  return {
    id: opts.id,
    properties: {
      Status: { type: "select" as const, select: { name: opts.status } },
      "Application Date": {
        type: "date" as const,
        date: opts.appDate ? { start: opts.appDate } : null,
      },
      "Date Scraped": {
        type: "date" as const,
        date: scraped ? { start: scraped } : null,
      },
    },
  };
}

function mockClient(
  pages: ReturnType<typeof makePage>[],
  opts: { pageSize?: number; failOn?: Set<string> } = {},
) {
  const trashed: string[] = [];
  const calls = { query: 0 };
  const pageSize = opts.pageSize ?? (pages.length || 1);
  const client = {
    databases: {
      query: async ({ start_cursor }: { start_cursor?: string }) => {
        calls.query++;
        const offset = start_cursor ? Number(start_cursor) : 0;
        const slice = pages.slice(offset, offset + pageSize);
        const next = offset + pageSize;
        const hasMore = next < pages.length;
        return { results: slice, has_more: hasMore, next_cursor: hasMore ? String(next) : null };
      },
    },
    pages: {
      update: async ({ page_id }: { page_id: string }) => {
        if (opts.failOn?.has(page_id)) throw new Error("trash failed");
        trashed.push(page_id);
        return {};
      },
    },
  } as unknown as ResilientNotionClient;
  return { client, trashed, calls };
}

describe("prune", () => {
  test("trashes prunable pages and keeps protected ones", async () => {
    const { client, trashed } = mockClient([
      makePage({ id: "prune-me", status: "To Review" }),
      makePage({ id: "blocked", status: "Company Blocked" }),
      makePage({ id: "locked", status: "Applied", appDate: TODAY }),
      makePage({ id: "no-date", status: "To Review", scraped: null }),
    ]);

    const stats = await prune(client, "db");

    expect(trashed).toEqual(["prune-me"]);
    expect(stats).toEqual({
      scanned: 4,
      pruned: 1,
      failed: 0,
      keptBlocked: 1,
      keptLocked: 1,
      keptNoDate: 1,
    });
  });

  test("counts a trash failure and keeps going", async () => {
    const { client, trashed } = mockClient(
      [makePage({ id: "fails", status: "To Review" }), makePage({ id: "ok", status: "To Review" })],
      { failOn: new Set(["fails"]) },
    );

    const stats = await prune(client, "db");

    expect(trashed).toEqual(["ok"]);
    expect(stats.pruned).toBe(1);
    expect(stats.failed).toBe(1);
  });

  test("paginates across has_more", async () => {
    const { client, trashed, calls } = mockClient(
      [
        makePage({ id: "a", status: "To Review" }),
        makePage({ id: "b", status: "To Review" }),
        makePage({ id: "c", status: "To Review" }),
      ],
      { pageSize: 1 },
    );

    const stats = await prune(client, "db");

    expect(calls.query).toBe(3);
    expect(stats.scanned).toBe(3);
    expect(trashed.sort()).toEqual(["a", "b", "c"]);
  });
});
