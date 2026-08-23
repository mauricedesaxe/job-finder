import { describe, expect, test } from "bun:test";
import type { EvaluationCriteria } from "../../config/evaluation";
import type { JobListing } from "../../types";
import { evaluateJob, JobEvaluationSchema } from "../evaluate";

const job: JobListing = {
  title: "Senior Engineer",
  company: "TestCo",
  url: "https://example.test",
  source: "test",
  keywordsMatched: [],
  datePosted: null,
  dateScraped: "2026-01-01",
  description: "body",
  location: "Remote",
  profile: "",
};
const filter: EvaluationCriteria = {
  name: "role-quality",
  promptName: "job-finder-filter-role-quality",
};
const profile: EvaluationCriteria = {
  name: "ai-engineering",
  promptName: "job-finder-profile-ai-engineering",
};
describe("evaluation", () => {
  test("parses the code-owned tool contract", () =>
    expect(JobEvaluationSchema.parse({ pass: true, reason: "ok" })).toEqual({
      pass: true,
      reason: "ok",
    }));
  test("stops before profiles when a filter rejects", async () => {
    const calls: string[] = [];
    const result = await evaluateJob(job, {
      filters: [filter],
      profiles: [profile],
      evaluate: async (_job, criteria) => {
        calls.push(criteria.name);
        return { pass: false, reason: "no" };
      },
    });
    expect(result).toEqual({ pass: false, reason: "no" });
    expect(calls).toEqual(["role-quality"]);
  });
  test("uses the passing profile name", async () => {
    const result = await evaluateJob(job, {
      filters: [filter],
      profiles: [profile],
      evaluate: async () => ({ pass: true, reason: "yes" }),
    });
    expect(result).toEqual({ pass: true, reason: "yes", profileName: "ai-engineering" });
  });
});
