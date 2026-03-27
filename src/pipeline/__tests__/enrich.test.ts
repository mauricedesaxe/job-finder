import { describe, expect, test } from "bun:test";
import { enrichJob, type JobEnrichment } from "../enrich";

describe("enrich module exports", () => {
  test("exports enrichJob function", () => {
    expect(typeof enrichJob).toBe("function");
  });

  test("JobEnrichment interface shape is correct", () => {
    const enrichment: JobEnrichment = {
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
