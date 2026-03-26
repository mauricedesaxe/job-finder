import { test, expect, describe } from "bun:test";
import { buildSearchQuery, filterJobUrls } from "./search";

describe("buildSearchQuery", () => {
  test("builds site-scoped query", () => {
    const query = buildSearchQuery("defi", "jobs.ashbyhq.com");
    expect(query).toBe("site:jobs.ashbyhq.com defi");
  });

  test("handles multi-word keywords", () => {
    const query = buildSearchQuery("typescript backend", "boards.greenhouse.io");
    expect(query).toBe("site:boards.greenhouse.io typescript backend");
  });
});

describe("filterJobUrls", () => {
  test("extracts matching URLs from results", () => {
    const results = [
      { title: "Job 1", url: "https://jobs.ashbyhq.com/acme/12345", description: "" },
      { title: "Job 2", url: "https://jobs.ashbyhq.com/acme/67890", description: "" },
      { title: "Other", url: "https://example.com/not-a-job", description: "" },
    ];
    const urls = filterJobUrls(results, "jobs.ashbyhq.com");
    expect(urls).toEqual([
      "https://jobs.ashbyhq.com/acme/12345",
      "https://jobs.ashbyhq.com/acme/67890",
    ]);
  });

  test("deduplicates URLs", () => {
    const results = [
      { title: "Job 1", url: "https://jobs.lever.co/company/abc", description: "" },
      { title: "Job 1 again", url: "https://jobs.lever.co/company/abc", description: "" },
    ];
    const urls = filterJobUrls(results, "jobs.lever.co");
    expect(urls).toHaveLength(1);
  });

  test("strips trailing punctuation", () => {
    const results = [
      { title: "Job", url: "https://boards.greenhouse.io/company/jobs/123.", description: "" },
    ];
    const urls = filterJobUrls(results, "boards.greenhouse.io");
    expect(urls).toEqual(["https://boards.greenhouse.io/company/jobs/123"]);
  });

  test("returns empty array when no matches", () => {
    const results = [
      { title: "Unrelated", url: "https://example.com/other", description: "" },
    ];
    const urls = filterJobUrls(results, "jobs.ashbyhq.com");
    expect(urls).toEqual([]);
  });
});
