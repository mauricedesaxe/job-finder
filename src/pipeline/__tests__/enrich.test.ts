import { test, expect, describe } from "bun:test";

const mod = await import("../enrich");

describe("enrich module exports", () => {
  test("exports enrichJob function", () => {
    expect(typeof mod.enrichJob).toBe("function");
  });

  test("JobEnrichment interface shape is correct", () => {
    const enrichment: mod.JobEnrichment = {
      title: "Engineer",
      company: "Acme",
      description: "Build things",
      location: "Remote",
    };
    expect(enrichment.title).toBe("Engineer");
    expect(enrichment.company).toBe("Acme");
    expect(enrichment.description).toBe("Build things");
    expect(enrichment.location).toBe("Remote");
  });
});
