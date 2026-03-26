import { test, expect, describe } from "bun:test";
import { buildSearchQuery, buildJinaUrl, extractJobUrls } from "./search";

describe("buildSearchQuery", () => {
  test("builds basic query for page 0", () => {
    const url = buildSearchQuery("defi", "jobs.ashbyhq.com", 0);
    expect(url).toContain("q=site%3Ajobs.ashbyhq.com+defi");
    expect(url).toContain("num=10");
    expect(url).not.toContain("start=");
  });

  test("includes start param for page > 0", () => {
    const url = buildSearchQuery("solana", "jobs.lever.co", 2);
    expect(url).toContain("start=20");
  });

  test("handles multi-word keywords", () => {
    const url = buildSearchQuery("typescript backend", "boards.greenhouse.io");
    expect(url).toContain("q=site%3Aboards.greenhouse.io+typescript+backend");
  });
});

describe("buildJinaUrl", () => {
  test("wraps target URL with Jina prefix", () => {
    const result = buildJinaUrl(
      "https://www.google.com/search?q=test",
      "https://r.jina.ai",
    );
    expect(result).toBe(
      "https://r.jina.ai/https://www.google.com/search?q=test",
    );
  });
});

describe("extractJobUrls", () => {
  test("extracts matching URLs from markdown", () => {
    const markdown = `
# Search Results
- [Software Engineer](https://jobs.ashbyhq.com/acme/12345)
- [Product Manager](https://jobs.ashbyhq.com/acme/67890)
- [Other link](https://example.com/not-a-job)
    `;
    const urls = extractJobUrls(markdown, "jobs.ashbyhq.com");
    expect(urls).toEqual([
      "https://jobs.ashbyhq.com/acme/12345",
      "https://jobs.ashbyhq.com/acme/67890",
    ]);
  });

  test("deduplicates URLs", () => {
    const markdown = `
[Job 1](https://jobs.lever.co/company/abc)
[Job 1 again](https://jobs.lever.co/company/abc)
    `;
    const urls = extractJobUrls(markdown, "jobs.lever.co");
    expect(urls).toHaveLength(1);
  });

  test("strips trailing punctuation", () => {
    const markdown =
      "Check out https://boards.greenhouse.io/company/jobs/123.";
    const urls = extractJobUrls(markdown, "boards.greenhouse.io");
    expect(urls).toEqual(["https://boards.greenhouse.io/company/jobs/123"]);
  });

  test("returns empty array when no matches", () => {
    const markdown = "No job listings found for this query.";
    const urls = extractJobUrls(markdown, "jobs.ashbyhq.com");
    expect(urls).toEqual([]);
  });
});
