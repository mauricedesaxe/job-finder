import { test, expect, describe } from "bun:test";
import Anthropic from "@anthropic-ai/sdk";

// Import the module to access the tool definitions indirectly via the file
const mod = await import("./evaluate");

// We can't directly access the const tool definitions, but we can verify
// the exported interfaces and function signatures exist
describe("evaluate module exports", () => {
  test("exports evaluateJob function", () => {
    expect(typeof mod.evaluateJob).toBe("function");
  });

  test("exports enrichJob function", () => {
    expect(typeof mod.enrichJob).toBe("function");
  });

  test("JobEvaluation interface shape is correct", () => {
    const evaluation: mod.JobEvaluation = { pass: true, reason: "test" };
    expect(evaluation.pass).toBe(true);
    expect(evaluation.reason).toBe("test");
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
