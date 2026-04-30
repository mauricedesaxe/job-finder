import { beforeEach, describe, expect, test } from "bun:test";
import { clearAshbyCache, detectAtsSource, fetchAtsData, formatAtsBlock } from "..";
import type { AtsJobData, Fetcher } from "../types";

import ledgerFixture from "./fixtures/ashby-ledger-org.json";
import openupFixture from "./fixtures/greenhouse-openup-senior-ai-engineer.json";
import yunoFixture from "./fixtures/lever-yuno-platform-engineer-ai.json";

function jsonFetcher(payload: unknown, status = 200): Fetcher {
  return async () =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

beforeEach(() => {
  clearAshbyCache();
});

describe("detectAtsSource", () => {
  test("detects lever", () => {
    expect(detectAtsSource("https://jobs.lever.co/foo/bar")).toBe("lever");
  });
  test("detects ashby", () => {
    expect(detectAtsSource("https://jobs.ashbyhq.com/foo/bar")).toBe("ashby");
  });
  test("detects greenhouse", () => {
    expect(detectAtsSource("https://boards.greenhouse.io/foo/jobs/1")).toBe("greenhouse");
  });
  test("returns null for unsupported source", () => {
    expect(detectAtsSource("https://apply.workable.com/foo/j/123")).toBeNull();
    expect(detectAtsSource("https://example.com/jobs")).toBeNull();
  });
});

describe("fetchAtsData", () => {
  test("dispatches to lever client", async () => {
    const result = await fetchAtsData(
      "https://jobs.lever.co/yuno/33309adb-efb0-414c-9e9a-da13435a0242",
      jsonFetcher(yunoFixture),
    );
    expect(result?.source).toBe("lever");
    expect(result?.location).toBe("Argentina");
  });

  test("dispatches to ashby client", async () => {
    const result = await fetchAtsData(
      "https://jobs.ashbyhq.com/ledger/4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
      jsonFetcher(ledgerFixture),
    );
    expect(result?.source).toBe("ashby");
    expect(result?.location).toBe("Paris, France");
  });

  test("dispatches to greenhouse client", async () => {
    const result = await fetchAtsData(
      "https://boards.greenhouse.io/openup/jobs/4847917101",
      jsonFetcher(openupFixture),
    );
    expect(result?.source).toBe("greenhouse");
    expect(result?.location).toBe("Amsterdam");
  });

  test("returns null for unsupported URL", async () => {
    const result = await fetchAtsData("https://apply.workable.com/foo/j/123", jsonFetcher({}));
    expect(result).toBeNull();
  });
});

describe("formatAtsBlock", () => {
  test("formats a complete Lever response", () => {
    const data: AtsJobData = {
      source: "lever",
      location: "Argentina",
      locations: ["Argentina", "Europe", "Spain"],
      workplaceType: "Remote",
      country: "AR",
    };
    const block = formatAtsBlock(data);
    expect(block).toContain("## ATS Structured Data (from lever API)");
    expect(block).toContain("- Primary location: Argentina");
    expect(block).toContain("- All listed locations: Argentina, Europe, Spain");
    expect(block).toContain("- Workplace type: Remote");
    expect(block).toContain("- Country (HQ): AR");
    expect(block).toContain("not final eligibility");
    expect(block.endsWith("---")).toBe(true);
  });

  test("omits empty fields", () => {
    const data: AtsJobData = {
      source: "greenhouse",
      location: "",
      locations: [],
      workplaceType: null,
      country: null,
    };
    const block = formatAtsBlock(data);
    expect(block).not.toContain("- Primary location:");
    expect(block).not.toContain("- All listed locations:");
    expect(block).not.toContain("- Country (HQ):");
    // Workplace type always shown (with "unspecified" fallback)
    expect(block).toContain("- Workplace type: unspecified");
  });

  test("preserves the eligibility-disclaimer line so the LLM does not over-index on country", () => {
    const data: AtsJobData = {
      source: "lever",
      location: "Argentina",
      locations: ["Argentina", "Europe"],
      workplaceType: "Remote",
      country: "AR",
    };
    const block = formatAtsBlock(data);
    expect(block).toContain("primary location may be HQ");
  });
});
