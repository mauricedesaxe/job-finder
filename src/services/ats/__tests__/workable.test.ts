import { describe, expect, test } from "bun:test";
import type { Fetcher, WorkableListResponse } from "../types";
import { fetchWorkableJob, parseWorkableUrl } from "../workable";

import v2aiFixture from "./fixtures/workable-v2-ai-listing.json";

function jsonFetcher(payload: unknown, status = 200): Fetcher {
  return async () =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

describe("parseWorkableUrl", () => {
  test("extracts slug and shortcode from canonical URL", () => {
    expect(parseWorkableUrl("https://apply.workable.com/v2-ai/j/CF51DE915D/")).toEqual({
      slug: "v2-ai",
      shortcode: "CF51DE915D",
    });
  });

  test("extracts slug and shortcode without trailing slash", () => {
    expect(parseWorkableUrl("https://apply.workable.com/devsinc-17/j/EAAD0EEF7C")).toEqual({
      slug: "devsinc-17",
      shortcode: "EAAD0EEF7C",
    });
  });

  test("returns null for non-Workable host", () => {
    expect(parseWorkableUrl("https://example.com/v2-ai/j/CF51DE915D/")).toBeNull();
  });

  test("returns null when /j/ segment is missing", () => {
    expect(parseWorkableUrl("https://apply.workable.com/v2-ai/")).toBeNull();
  });

  test("returns null when shortcode is missing", () => {
    expect(parseWorkableUrl("https://apply.workable.com/v2-ai/j/")).toBeNull();
  });

  test("returns null on malformed URL", () => {
    expect(parseWorkableUrl("not a url")).toBeNull();
  });
});

describe("fetchWorkableJob", () => {
  test("normalizes the recorded V2 AI response (Hybrid + Sydney AU)", async () => {
    const result = await fetchWorkableJob(
      "https://apply.workable.com/v2-ai/j/CF51DE915D/",
      "Full Stack AI Principal Engineer",
      jsonFetcher(v2aiFixture),
    );

    expect(result).toEqual({
      source: "workable",
      location: "Sydney, Australia",
      locations: ["Sydney, Australia"],
      workplaceType: "Hybrid",
      country: "Australia",
    });
  });

  test("normalizes workplace value variants", async () => {
    const make = (workplace: string): WorkableListResponse => ({
      results: [{ shortcode: "X1", title: "T", workplace, location: { country: "Spain" } }],
    });

    const onsite = await fetchWorkableJob(
      "https://apply.workable.com/x/j/X1",
      "T",
      jsonFetcher(make("on_site")),
    );
    const hybrid = await fetchWorkableJob(
      "https://apply.workable.com/x/j/X1",
      "T",
      jsonFetcher(make("hybrid")),
    );
    const remote = await fetchWorkableJob(
      "https://apply.workable.com/x/j/X1",
      "T",
      jsonFetcher(make("remote")),
    );

    expect(onsite?.workplaceType).toBe("OnSite");
    expect(hybrid?.workplaceType).toBe("Hybrid");
    expect(remote?.workplaceType).toBe("Remote");
  });

  test("returns null on unknown workplace value", async () => {
    const result = await fetchWorkableJob(
      "https://apply.workable.com/x/j/X1",
      "T",
      jsonFetcher({
        results: [{ shortcode: "X1", title: "T", workplace: "elsewhere" }],
      }),
    );
    expect(result?.workplaceType).toBeNull();
  });

  test("returns null when the shortcode is not in the query results", async () => {
    // Title query returned 10 jobs, but none with our shortcode — graceful
    // degradation to body-only evaluation.
    const result = await fetchWorkableJob(
      "https://apply.workable.com/x/j/MISSING",
      "Generic Senior Engineer",
      jsonFetcher({
        results: [{ shortcode: "OTHER1", title: "T", workplace: "remote" }],
      }),
    );
    expect(result).toBeNull();
  });

  test("falls through gracefully on bad URL", async () => {
    const result = await fetchWorkableJob("https://example.com/foo/bar", "T", jsonFetcher({}));
    expect(result).toBeNull();
  });

  test("returns null on non-200 response", async () => {
    const result = await fetchWorkableJob(
      "https://apply.workable.com/x/j/X1",
      "T",
      jsonFetcher({}, 500),
    );
    expect(result).toBeNull();
  });

  test("returns null on network error", async () => {
    const failing: Fetcher = async () => {
      throw new Error("network down");
    };
    const result = await fetchWorkableJob("https://apply.workable.com/x/j/X1", "T", failing);
    expect(result).toBeNull();
  });

  test("returns null on invalid JSON", async () => {
    const badJson: Fetcher = async () =>
      new Response("not json", { status: 200, headers: { "Content-Type": "text/plain" } });
    const result = await fetchWorkableJob("https://apply.workable.com/x/j/X1", "T", badJson);
    expect(result).toBeNull();
  });

  test("handles missing location gracefully (only locations[] present)", async () => {
    const result = await fetchWorkableJob(
      "https://apply.workable.com/x/j/X1",
      "T",
      jsonFetcher({
        results: [
          {
            shortcode: "X1",
            title: "T",
            workplace: "remote",
            locations: [{ city: "Berlin", country: "Germany" }],
          },
        ],
      }),
    );
    expect(result).toEqual({
      source: "workable",
      location: "",
      locations: ["Berlin, Germany"],
      workplaceType: "Remote",
      country: null,
    });
  });

  test("POSTs the title as the query body", async () => {
    let captured: { url?: string; init?: RequestInit } = {};
    const capturing: Fetcher = async (url, init) => {
      captured = { url, init };
      return new Response(JSON.stringify({ results: [] }), { status: 200 });
    };
    await fetchWorkableJob(
      "https://apply.workable.com/v2-ai/j/CF51DE915D/",
      "Full Stack AI Principal Engineer",
      capturing,
    );
    expect(captured.url).toBe("https://apply.workable.com/api/v3/accounts/v2-ai/jobs");
    expect(captured.init?.method).toBe("POST");
    expect(captured.init?.body).toBe(JSON.stringify({ query: "Full Stack AI Principal Engineer" }));
  });
});
