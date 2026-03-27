import { test, expect, describe } from "bun:test";
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
        rich_text: opts.company
          ? [{ plain_text: opts.company }]
          : [],
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
  } as any;
}

describe("buildNotionCache", () => {
  test("collects existing URLs", async () => {
    const client = mockClient([
      makePage({ url: "https://example.com/job1" }),
      makePage({ url: "https://example.com/job2" }),
    ]);
    const cache = await buildNotionCache(client, "db-id");
    expect(cache.existingUrls.size).toBe(2);
    expect(cache.existingUrls.has("https://example.com/job1")).toBe(true);
  });

  test("collects blocked companies", async () => {
    const client = mockClient([
      makePage({ company: "BadCorp", status: "Company Blocked" }),
      makePage({ company: "GoodCorp", status: "To Review" }),
    ]);
    const cache = await buildNotionCache(client, "db-id");
    expect(cache.blockedCompanies.has("BadCorp")).toBe(true);
    expect(cache.blockedCompanies.has("GoodCorp")).toBe(false);
  });

  test("collects recent application companies", async () => {
    const recentDate = new Date();
    recentDate.setMonth(recentDate.getMonth() - 1);
    const oldDate = new Date();
    oldDate.setMonth(oldDate.getMonth() - 8);

    const client = mockClient([
      makePage({
        company: "RecentCo",
        appDate: recentDate.toISOString().split("T")[0],
      }),
      makePage({
        company: "OldCo",
        appDate: oldDate.toISOString().split("T")[0],
      }),
    ]);
    const cache = await buildNotionCache(client, "db-id");
    expect(cache.recentAppCompanies.has("RecentCo")).toBe(true);
    expect(cache.recentAppCompanies.has("OldCo")).toBe(false);
  });

  test("builds jobs by company index", async () => {
    const client = mockClient([
      makePage({ company: "Acme", title: "Senior Engineer" }),
      makePage({ company: "Acme", title: "Staff Engineer" }),
      makePage({ company: "Other", title: "Designer" }),
    ]);
    const cache = await buildNotionCache(client, "db-id");
    expect(cache.jobsByCompany.get("Acme")).toEqual([
      "Senior Engineer",
      "Staff Engineer",
    ]);
    expect(cache.jobsByCompany.get("Other")).toEqual(["Designer"]);
  });

  test("handles empty database", async () => {
    const client = mockClient([]);
    const cache = await buildNotionCache(client, "db-id");
    expect(cache.existingUrls.size).toBe(0);
    expect(cache.blockedCompanies.size).toBe(0);
    expect(cache.recentAppCompanies.size).toBe(0);
    expect(cache.jobsByCompany.size).toBe(0);
  });
});
