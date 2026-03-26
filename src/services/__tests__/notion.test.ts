import { test, expect, describe } from "bun:test";
import { buildNotionProperties, descriptionToBlocks } from "../notion";
import type { JobListing } from "../../../types";

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
    location: "Remote (Global)",
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
    expect(props.Location).toEqual({
      rich_text: [{ text: { content: "Remote (Global)" } }],
    });
    expect(props.Status).toEqual({ select: { name: "To Review" } });
  });

  test("omits Date Posted when null", () => {
    const jobNoDate = { ...job, datePosted: null };
    const props = buildNotionProperties(jobNoDate);
    expect(props["Date Posted"]).toBeUndefined();
  });

  test("omits Location when empty", () => {
    const jobNoLocation = { ...job, location: "" };
    const props = buildNotionProperties(jobNoLocation);
    expect(props.Location).toBeUndefined();
  });
});

describe("descriptionToBlocks", () => {
  test("splits paragraphs into separate blocks", () => {
    const desc = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.";
    const blocks = descriptionToBlocks(desc);
    expect(blocks).toHaveLength(3);
    expect(blocks[0].paragraph.rich_text[0].text.content).toBe("First paragraph.");
    expect(blocks[1].paragraph.rich_text[0].text.content).toBe("Second paragraph.");
    expect(blocks[2].paragraph.rich_text[0].text.content).toBe("Third paragraph.");
  });

  test("chunks oversized paragraphs at 2000 chars", () => {
    const longText = "x".repeat(4500);
    const blocks = descriptionToBlocks(longText);
    expect(blocks).toHaveLength(3);
    expect(blocks[0].paragraph.rich_text[0].text.content).toHaveLength(2000);
    expect(blocks[1].paragraph.rich_text[0].text.content).toHaveLength(2000);
    expect(blocks[2].paragraph.rich_text[0].text.content).toHaveLength(500);
  });

  test("returns empty array for empty description", () => {
    const blocks = descriptionToBlocks("");
    expect(blocks).toHaveLength(0);
  });

  test("strips blank paragraphs", () => {
    const desc = "First.\n\n\n\n\nSecond.";
    const blocks = descriptionToBlocks(desc);
    expect(blocks).toHaveLength(2);
  });
});
