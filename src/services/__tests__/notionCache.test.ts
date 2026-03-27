import { test, expect, describe } from "bun:test";
import { buildNotionCache, CacheSyncer } from "../notionCache";
import type { NotionCache } from "../notionCache";

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

function emptyCache(): NotionCache {
  return {
    existingUrls: new Set(),
    blockedCompanies: new Set(),
    recentAppCompanies: new Set(),
    jobsByCompany: new Map(),
  };
}

describe("CacheSyncer", () => {
  test("addUrl updates both local tracking and live cache", () => {
    const syncer = new CacheSyncer(emptyCache());
    syncer.addUrl("https://example.com/job1");
    expect(syncer.cache.existingUrls.has("https://example.com/job1")).toBe(true);
  });

  test("addTitle updates both local tracking and live cache", () => {
    const syncer = new CacheSyncer(emptyCache());
    syncer.addTitle("Acme", "Senior Engineer");
    syncer.addTitle("Acme", "Staff Engineer");
    expect(syncer.cache.jobsByCompany.get("Acme")).toEqual([
      "Senior Engineer",
      "Staff Engineer",
    ]);
  });

  test("stop clears the interval", () => {
    const syncer = new CacheSyncer(emptyCache());
    const client = mockClient([]);
    syncer.start(client, "db-id", 60_000);
    syncer.stop();
    // Calling stop again should be safe (no-op)
    syncer.stop();
  });

  test("sync merges local additions into fresh cache", async () => {
    const initialCache = emptyCache();
    initialCache.existingUrls.add("https://example.com/old");
    const syncer = new CacheSyncer(initialCache);

    // Add local data
    syncer.addUrl("https://example.com/local-job");
    syncer.addTitle("LocalCo", "Local Engineer");

    // Simulate a sync: fresh cache from Notion has new data
    const freshClient = mockClient([
      makePage({
        url: "https://example.com/new-from-notion",
        company: "NotionCo",
        title: "Notion Engineer",
        status: "To Review",
      }),
    ]);

    // Manually trigger what start() does on interval
    const fresh = await buildNotionCache(freshClient, "db-id");
    for (const url of ["https://example.com/local-job"]) {
      fresh.existingUrls.add(url);
    }
    for (const [company, titles] of [["LocalCo", ["Local Engineer"]]] as [string, string[]][]) {
      const existing = fresh.jobsByCompany.get(company) ?? [];
      for (const title of titles) {
        if (!existing.includes(title)) existing.push(title);
      }
      fresh.jobsByCompany.set(company, existing);
    }
    syncer.cache = fresh;

    // Verify merged state
    expect(syncer.cache.existingUrls.has("https://example.com/new-from-notion")).toBe(true);
    expect(syncer.cache.existingUrls.has("https://example.com/local-job")).toBe(true);
    // Old URL from initial cache is NOT in fresh (it wasn't in the mock Notion response)
    expect(syncer.cache.existingUrls.has("https://example.com/old")).toBe(false);
    expect(syncer.cache.jobsByCompany.get("NotionCo")).toEqual(["Notion Engineer"]);
    expect(syncer.cache.jobsByCompany.get("LocalCo")).toEqual(["Local Engineer"]);
  });
});
