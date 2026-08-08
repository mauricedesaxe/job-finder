import { describe, expect, test } from "bun:test";
import { buildNotionCache } from "../notionCache";

function makePage(opts: {
  url?: string;
  company?: string;
  title?: string;
  status?: string;
  appDate?: string | null;
}) {
  return {
    id: "page-id",
    properties: {
      URL: { type: "url" as const, url: opts.url ?? null },
      Company: {
        type: "rich_text" as const,
        rich_text: opts.company ? [{ plain_text: opts.company }] : [],
      },
      "Job Title": {
        type: "title" as const,
        title: opts.title ? [{ plain_text: opts.title }] : [],
      },
      Status: {
        type: "select" as const,
        select: opts.status ? { name: opts.status } : null,
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
  test("collects recent application companies within the window", async () => {
    const recentDate = new Date();
    recentDate.setMonth(recentDate.getMonth() - 1);
    const oldDate = new Date();
    oldDate.setMonth(oldDate.getMonth() - 8);

    const client = mockClient([
      makePage({ company: "RecentCo", appDate: recentDate.toISOString().split("T")[0] }),
      makePage({ company: "OldCo", appDate: oldDate.toISOString().split("T")[0] }),
    ]);
    const { cache } = await buildNotionCache(client, "db-id");
    expect(cache.recentAppCompanies.has("RecentCo")).toBe(true);
    expect(cache.recentAppCompanies.has("OldCo")).toBe(false);
  });

  test("collects every page's identity for the ledger backfill", async () => {
    const client = mockClient([
      makePage({ url: "https://e.com/a", company: "Acme", title: "Engineer", status: "To Review" }),
      makePage({ url: "https://e.com/b", company: "Acme", title: "Staff", status: "Rejected" }),
      makePage({ company: "NoUrl", title: "Engineer" }),
    ]);
    const { identities } = await buildNotionCache(client, "db-id");
    expect(identities).toHaveLength(3);
    expect(identities[0]?.url).toBe("https://e.com/a");
    expect(identities[1]?.title).toBe("Staff");
    expect(identities[2]?.url).toBe("");
  });

  test("handles an empty database", async () => {
    const client = mockClient([]);
    const { cache, identities } = await buildNotionCache(client, "db-id");
    expect(cache.recentAppCompanies.size).toBe(0);
    expect(identities).toHaveLength(0);
  });
});
