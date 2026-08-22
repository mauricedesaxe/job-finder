import { describe, expect, test } from "bun:test";
import { buildNotionCache } from "../notionCache";

function makePage(opts: { company?: string; appDate?: string | null }) {
  return {
    id: "page-id",
    properties: {
      Company: {
        type: "rich_text" as const,
        rich_text: opts.company ? [{ plain_text: opts.company }] : [],
      },
      "Application Date": {
        type: "date" as const,
        date: opts.appDate ? { start: opts.appDate } : null,
      },
    },
  };
}

function mockClient(pages: ReturnType<typeof makePage>[]) {
  return {
    databases: {
      query: async () => ({
        results: pages,
        has_more: false,
        next_cursor: null,
      }),
    },
  } as unknown as import("../notion/client").ResilientNotionClient;
}

describe("buildNotionCache", () => {
  test("collects recent application companies", async () => {
    const recentDate = new Date();
    recentDate.setMonth(recentDate.getMonth() - 1);
    const oldDate = new Date();
    oldDate.setMonth(oldDate.getMonth() - 8);

    const cache = await buildNotionCache(
      mockClient([
        makePage({
          company: "RecentCo",
          appDate: recentDate.toISOString().split("T")[0] ?? "",
        }),
        makePage({
          company: "OldCo",
          appDate: oldDate.toISOString().split("T")[0] ?? "",
        }),
      ]),
      "db-id",
    );

    expect(cache.recentAppCompanies.has("RecentCo")).toBe(true);
    expect(cache.recentAppCompanies.has("OldCo")).toBe(false);
  });

  test("returns an empty recent application set for an empty database", async () => {
    const cache = await buildNotionCache(mockClient([]), "db-id");

    expect(cache.recentAppCompanies.size).toBe(0);
  });
});
