import { describe, expect, test } from "bun:test";
import { fetchLeverJob, parseLeverUrl } from "../lever";
import type { Fetcher } from "../types";

import yunoFixture from "./fixtures/lever-yuno-platform-engineer-ai.json";

function jsonFetcher(payload: unknown, status = 200): Fetcher {
  return async () =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

describe("parseLeverUrl", () => {
  test("extracts org and id from canonical URL", () => {
    expect(parseLeverUrl("https://jobs.lever.co/yuno/abc-123")).toEqual({
      org: "yuno",
      id: "abc-123",
    });
  });

  test("returns null for non-Lever host", () => {
    expect(parseLeverUrl("https://example.com/yuno/abc")).toBeNull();
  });

  test("returns null when path is missing org or id", () => {
    expect(parseLeverUrl("https://jobs.lever.co/yuno")).toBeNull();
    expect(parseLeverUrl("https://jobs.lever.co/")).toBeNull();
  });

  test("returns null on malformed URL", () => {
    expect(parseLeverUrl("not a url")).toBeNull();
  });
});

describe("fetchLeverJob", () => {
  test("normalizes the recorded Yuno response (Argentina HQ + Europe in allLocations)", async () => {
    const result = await fetchLeverJob(
      "https://jobs.lever.co/yuno/33309adb-efb0-414c-9e9a-da13435a0242",
      jsonFetcher(yunoFixture),
    );

    expect(result).toEqual({
      source: "lever",
      location: "Argentina",
      locations: [
        "Argentina",
        "Bogota",
        "Chile",
        "Mexico",
        "Colombia",
        "Buenos Aires",
        "Europe",
        "Lima",
        "Paraguay",
        "Spain",
        "Amsterdam",
        "Belgium",
        "Brazil",
        "Germany",
        "Italy",
      ],
      workplaceType: "Remote",
      country: "AR",
    });
  });

  test("normalizes workplaceType case variants", async () => {
    const remote = await fetchLeverJob(
      "https://jobs.lever.co/x/y",
      jsonFetcher({ workplaceType: "remote" }),
    );
    const hybrid = await fetchLeverJob(
      "https://jobs.lever.co/x/y",
      jsonFetcher({ workplaceType: "hybrid" }),
    );
    const onsite = await fetchLeverJob(
      "https://jobs.lever.co/x/y",
      jsonFetcher({ workplaceType: "on-site" }),
    );

    expect(remote?.workplaceType).toBe("Remote");
    expect(hybrid?.workplaceType).toBe("Hybrid");
    expect(onsite?.workplaceType).toBe("OnSite");
  });

  test("returns null on unknown workplaceType value", async () => {
    const result = await fetchLeverJob(
      "https://jobs.lever.co/x/y",
      jsonFetcher({ workplaceType: "elsewhere" }),
    );
    expect(result?.workplaceType).toBeNull();
  });

  test("returns null on bad URL", async () => {
    const result = await fetchLeverJob("https://example.com/foo/bar", jsonFetcher({}, 200));
    expect(result).toBeNull();
  });

  test("returns null on non-200 response", async () => {
    const result = await fetchLeverJob("https://jobs.lever.co/x/y", jsonFetcher({}, 404));
    expect(result).toBeNull();
  });

  test("returns null on network error", async () => {
    const failing: Fetcher = async () => {
      throw new Error("network down");
    };
    const result = await fetchLeverJob("https://jobs.lever.co/x/y", failing);
    expect(result).toBeNull();
  });

  test("returns null on invalid JSON", async () => {
    const badJson: Fetcher = async () =>
      new Response("not json", { status: 200, headers: { "Content-Type": "text/plain" } });
    const result = await fetchLeverJob("https://jobs.lever.co/x/y", badJson);
    expect(result).toBeNull();
  });

  test("handles missing categories gracefully", async () => {
    const result = await fetchLeverJob(
      "https://jobs.lever.co/x/y",
      jsonFetcher({ workplaceType: "remote", country: "US" }),
    );
    expect(result).toEqual({
      source: "lever",
      location: "",
      locations: [],
      workplaceType: "Remote",
      country: "US",
    });
  });
});
