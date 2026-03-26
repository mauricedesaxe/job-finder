import { test, expect, describe } from "bun:test";
import { buildNotionProperties } from "./notion";
import type { JobListing } from "./types";

describe("buildNotionProperties", () => {
  const job: JobListing = {
    title: "Senior DeFi Engineer",
    company: "acme",
    url: "https://jobs.ashbyhq.com/acme/12345",
    source: "ashbyhq",
    keywordsMatched: ["defi", "solana"],
    datePosted: "2025-03-01",
    dateScraped: "2025-03-26",
    description: "Full job description here...",
  };

  test("maps all fields correctly", () => {
    const props = buildNotionProperties(job);

    expect(props["Job Title"]).toEqual({
      title: [{ text: { content: "Senior DeFi Engineer" } }],
    });
    expect(props.Company).toEqual({
      rich_text: [{ text: { content: "acme" } }],
    });
    expect(props.URL).toEqual({ url: "https://jobs.ashbyhq.com/acme/12345" });
    expect(props.Source).toEqual({ select: { name: "ashbyhq" } });
    expect(props.Keywords).toEqual({
      multi_select: [{ name: "defi" }, { name: "solana" }],
    });
    expect(props["Date Scraped"]).toEqual({ date: { start: "2025-03-26" } });
    expect(props["Date Posted"]).toEqual({ date: { start: "2025-03-01" } });
    expect(props.Status).toEqual({ select: { name: "To Review" } });
  });

  test("omits Date Posted when null", () => {
    const jobNoDate = { ...job, datePosted: null };
    const props = buildNotionProperties(jobNoDate);
    expect(props["Date Posted"]).toBeUndefined();
  });
});
