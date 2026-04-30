import { describe, expect, test } from "bun:test";
import { fetchGreenhouseJob, parseGreenhouseUrl } from "../greenhouse";
import type { Fetcher } from "../types";

import openupFixture from "./fixtures/greenhouse-openup-senior-ai-engineer.json";

function jsonFetcher(payload: unknown, status = 200): Fetcher {
  return async () =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

describe("parseGreenhouseUrl", () => {
  test("extracts from boards.greenhouse.io", () => {
    expect(parseGreenhouseUrl("https://boards.greenhouse.io/openup/jobs/4847917101")).toEqual({
      org: "openup",
      id: "4847917101",
    });
  });

  test("extracts from job-boards.greenhouse.io variant", () => {
    expect(parseGreenhouseUrl("https://job-boards.greenhouse.io/openup/jobs/4847917101")).toEqual({
      org: "openup",
      id: "4847917101",
    });
  });

  test("extracts from boards.eu.greenhouse.io variant", () => {
    expect(parseGreenhouseUrl("https://boards.eu.greenhouse.io/openup/jobs/4847917101")).toEqual({
      org: "openup",
      id: "4847917101",
    });
  });

  test("returns null for non-Greenhouse host", () => {
    expect(parseGreenhouseUrl("https://example.com/foo/jobs/123")).toBeNull();
  });

  test("returns null when /jobs/ segment missing", () => {
    expect(parseGreenhouseUrl("https://boards.greenhouse.io/openup/123")).toBeNull();
  });

  test("returns null when id is missing after /jobs/", () => {
    expect(parseGreenhouseUrl("https://boards.greenhouse.io/openup/jobs/")).toBeNull();
  });
});

describe("fetchGreenhouseJob", () => {
  test("normalizes the recorded OpenUp response (Amsterdam)", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/openup/jobs/4847917101",
      jsonFetcher(openupFixture),
    );

    expect(result).toEqual({
      source: "greenhouse",
      location: "Amsterdam",
      locations: ["Amsterdam", "Amsterdam, North Holland, Netherlands"],
      workplaceType: null,
      country: "Netherlands",
    });
  });

  test("workplaceType is always null (Greenhouse exposes no field)", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/foo/jobs/1",
      jsonFetcher({
        id: 1,
        location: { name: "Remote" },
      }),
    );
    expect(result?.workplaceType).toBeNull();
  });

  test("derives country from last comma-segment of offices[0].location", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/foo/jobs/1",
      jsonFetcher({
        id: 1,
        location: { name: "Remote" },
        offices: [{ id: 1, name: "HQ", location: "Berlin, Germany" }],
      }),
    );
    expect(result?.country).toBe("Germany");
  });

  test("country is null when offices[0].location has no comma", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/foo/jobs/1",
      jsonFetcher({
        id: 1,
        location: { name: "Remote" },
        offices: [{ id: 1, location: "Remote" }],
      }),
    );
    expect(result?.country).toBeNull();
  });

  test("country is null when no offices", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/foo/jobs/1",
      jsonFetcher({ id: 1, location: { name: "Remote" } }),
    );
    expect(result?.country).toBeNull();
  });

  test("returns null on bad URL", async () => {
    const result = await fetchGreenhouseJob("https://example.com/foo/jobs/1", jsonFetcher({}, 200));
    expect(result).toBeNull();
  });

  test("returns null on non-200 response", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/foo/jobs/1",
      jsonFetcher({}, 404),
    );
    expect(result).toBeNull();
  });

  test("returns null on schema mismatch (missing required id)", async () => {
    const result = await fetchGreenhouseJob(
      "https://boards.greenhouse.io/foo/jobs/1",
      jsonFetcher({ location: { name: "x" } }),
    );
    expect(result).toBeNull();
  });
});
