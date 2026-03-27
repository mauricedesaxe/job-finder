import { describe, expect, test } from "bun:test";
import {
  detectSource,
  extractCompanyFromUrl,
  extractDatePosted,
  extractTitle,
  parseJobDetails,
} from "../scrape";

describe("extractCompanyFromUrl", () => {
  test("extracts from Ashby URL", () => {
    expect(extractCompanyFromUrl("https://jobs.ashbyhq.com/acme/12345")).toBe("acme");
  });

  test("extracts from Lever URL", () => {
    expect(extractCompanyFromUrl("https://jobs.lever.co/mycompany/abc-def")).toBe("mycompany");
  });

  test("extracts from Greenhouse URL", () => {
    expect(extractCompanyFromUrl("https://boards.greenhouse.io/coolstartup/jobs/123")).toBe(
      "coolstartup",
    );
  });

  test("returns Unknown for invalid URL", () => {
    expect(extractCompanyFromUrl("not-a-url")).toBe("Unknown");
  });

  test("returns Unknown for URL with no path", () => {
    expect(extractCompanyFromUrl("https://jobs.ashbyhq.com/")).toBe("Unknown");
  });
});

describe("detectSource", () => {
  test("detects ashbyhq", () => {
    expect(detectSource("https://jobs.ashbyhq.com/acme/123")).toBe("ashbyhq");
  });

  test("detects lever", () => {
    expect(detectSource("https://jobs.lever.co/company/abc")).toBe("lever");
  });

  test("detects greenhouse", () => {
    expect(detectSource("https://boards.greenhouse.io/co/jobs/1")).toBe("greenhouse");
  });

  test("detects workable", () => {
    expect(detectSource("https://apply.workable.com/mlabs/j/C07B32BD46")).toBe("workable");
  });

  test("returns other for unknown domain", () => {
    expect(detectSource("https://example.com/jobs/1")).toBe("other");
  });
});

describe("extractTitle", () => {
  test("extracts from # heading", () => {
    const md = "# Senior Software Engineer\n\nSome description...";
    expect(extractTitle(md)).toBe("Senior Software Engineer");
  });

  test("extracts from bold text as fallback", () => {
    const md = "Welcome to our job page\n\n**Backend Developer** at Acme";
    expect(extractTitle(md)).toBe("Backend Developer");
  });

  test("returns Unknown Position when no title found", () => {
    const md = "This page has no clear title formatting.";
    expect(extractTitle(md)).toBe("Unknown Position");
  });
});

describe("extractDatePosted", () => {
  test("extracts date in 'Month DD, YYYY' format", () => {
    const md = "Posted on January 15, 2025\n\nJob description...";
    expect(extractDatePosted(md)).toBe("2025-01-15");
  });

  test("extracts ISO date format", () => {
    const md = "Published: 2025-03-20\n\nDetails...";
    expect(extractDatePosted(md)).toBe("2025-03-20");
  });

  test("returns null when no date found", () => {
    const md = "# Job Title\n\nNo date information here.";
    expect(extractDatePosted(md)).toBeNull();
  });
});

describe("parseJobDetails", () => {
  test("parses complete job listing", () => {
    const md = "# DeFi Protocol Engineer\n\nPosted on March 1, 2025\n\nBuild stuff.";
    const job = parseJobDetails(md, "https://jobs.ashbyhq.com/acme/12345", "defi");

    expect(job.title).toBe("DeFi Protocol Engineer");
    expect(job.company).toBe("acme");
    expect(job.url).toBe("https://jobs.ashbyhq.com/acme/12345");
    expect(job.source).toBe("ashbyhq");
    expect(job.keywordsMatched).toEqual(["defi"]);
    expect(job.datePosted).toBe("2025-03-01");
    expect(job.dateScraped).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(job.description).toBe(md);
    expect(job.location).toBe("");
  });
});
