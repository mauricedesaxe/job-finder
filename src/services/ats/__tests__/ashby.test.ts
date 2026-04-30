import { beforeEach, describe, expect, test } from "bun:test";
import { clearAshbyCache, fetchAshbyJob, parseAshbyUrl } from "../ashby";
import type { Fetcher } from "../types";

import ledgerFixture from "./fixtures/ashby-ledger-org.json";

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

describe("parseAshbyUrl", () => {
  test("extracts org and id from canonical URL", () => {
    expect(parseAshbyUrl("https://jobs.ashbyhq.com/ledger/abc-123")).toEqual({
      org: "ledger",
      id: "abc-123",
    });
  });

  test("returns null for non-Ashby host", () => {
    expect(parseAshbyUrl("https://example.com/ledger/abc")).toBeNull();
  });

  test("returns null when path is missing org or id", () => {
    expect(parseAshbyUrl("https://jobs.ashbyhq.com/ledger")).toBeNull();
  });

  test("returns null on malformed URL", () => {
    expect(parseAshbyUrl("not a url")).toBeNull();
  });
});

describe("fetchAshbyJob", () => {
  test("finds the right job by id from the org listing (Ledger Tax Assistant)", async () => {
    const result = await fetchAshbyJob(
      "https://jobs.ashbyhq.com/ledger/4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
      jsonFetcher(ledgerFixture),
    );

    expect(result).toEqual({
      source: "ashby",
      location: "Paris, France",
      locations: ["Paris, France"],
      workplaceType: "OnSite",
      country: "France, Metropolitan",
    });
  });

  test("returns null when id is not found in org listing", async () => {
    const result = await fetchAshbyJob(
      "https://jobs.ashbyhq.com/ledger/00000000-0000-0000-0000-000000000000",
      jsonFetcher(ledgerFixture),
    );
    expect(result).toBeNull();
  });

  test("normalizes secondaryLocations and dedupes primary", async () => {
    const payload = {
      jobs: [
        {
          id: "job1",
          location: "Berlin",
          secondaryLocations: [{ location: "Berlin" }, { location: "Munich" }],
          workplaceType: "Hybrid",
        },
      ],
    };
    const result = await fetchAshbyJob("https://jobs.ashbyhq.com/foo/job1", jsonFetcher(payload));
    expect(result?.location).toBe("Berlin");
    expect(result?.locations).toEqual(["Berlin", "Munich"]);
  });

  test("caches org response across calls in the same run", async () => {
    let callCount = 0;
    const counting: Fetcher = async () => {
      callCount++;
      return new Response(JSON.stringify(ledgerFixture), { status: 200 });
    };

    await fetchAshbyJob(
      "https://jobs.ashbyhq.com/ledger/4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
      counting,
    );
    await fetchAshbyJob("https://jobs.ashbyhq.com/ledger/another-id", counting);

    expect(callCount).toBe(1);
  });

  test("clearAshbyCache forces a refetch", async () => {
    let callCount = 0;
    const counting: Fetcher = async () => {
      callCount++;
      return new Response(JSON.stringify(ledgerFixture), { status: 200 });
    };

    await fetchAshbyJob(
      "https://jobs.ashbyhq.com/ledger/4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
      counting,
    );
    clearAshbyCache();
    await fetchAshbyJob(
      "https://jobs.ashbyhq.com/ledger/4fabe068-ce5b-4962-abd5-1d9dbb7c63f8",
      counting,
    );

    expect(callCount).toBe(2);
  });

  test("returns null on bad URL", async () => {
    const result = await fetchAshbyJob("https://example.com/foo/bar", jsonFetcher({}, 200));
    expect(result).toBeNull();
  });

  test("returns null on non-200 response", async () => {
    const result = await fetchAshbyJob("https://jobs.ashbyhq.com/badorg/abc", jsonFetcher({}, 404));
    expect(result).toBeNull();
  });

  test("returns null on schema mismatch", async () => {
    const result = await fetchAshbyJob(
      "https://jobs.ashbyhq.com/foo/abc",
      jsonFetcher({ wrong: "shape" }),
    );
    expect(result).toBeNull();
  });

  test("accepts null for optional fields (Ashby returns null, not undefined)", async () => {
    const payload = {
      jobs: [
        {
          id: "job1",
          location: "Remote",
          secondaryLocations: null,
          isRemote: null,
          workplaceType: null,
          address: null,
        },
      ],
    };
    const result = await fetchAshbyJob("https://jobs.ashbyhq.com/foo/job1", jsonFetcher(payload));
    expect(result).toEqual({
      source: "ashby",
      location: "Remote",
      locations: ["Remote"],
      workplaceType: null,
      country: null,
    });
  });
});
