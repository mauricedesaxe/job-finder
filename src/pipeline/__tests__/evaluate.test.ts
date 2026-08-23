import { describe, expect, test } from "bun:test";
import {
  EVALUATION_PROFILES,
  type EvaluationCriteria,
  getEvaluationFilters,
} from "../../config/evaluation";
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
  name: "applied-ai-product-engineer",
  promptName: "job-finder-profile-applied-ai-product-engineer",
};
describe("evaluation", () => {
  test("parses the code-owned tool contract", () =>
    expect(JobEvaluationSchema.parse({ pass: true, reason: "ok" })).toEqual({
      pass: true,
      reason: "ok",
    }));
  test("keeps filters AND-ed and profiles OR-ed", async () => {
    const filterCalls: string[] = [];
    const profileCalls: string[] = [];
    const result = await evaluateJob(job, {
      filters: getEvaluationFilters(),
      profiles: EVALUATION_PROFILES,
      evaluate: async (_job, criteria) => {
        if (criteria.name.includes("product-engineer")) {
          profileCalls.push(criteria.name);
          return {
            pass: criteria.name === "applied-ai-product-engineer",
            reason: criteria.name,
          };
        }
        filterCalls.push(criteria.name);
        return { pass: true, reason: criteria.name };
      },
    });

    expect(filterCalls).toEqual([
      "remote-europe-eligible",
      "compensation-minimum",
      "role-quality",
      "cheap-shop-placement",
    ]);
    expect(profileCalls).toEqual(["early-stage-product-engineer", "applied-ai-product-engineer"]);
    expect(result.profileName).toBe("applied-ai-product-engineer");
  });

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
    expect(result).toEqual({
      pass: true,
      reason: "yes",
      profileName: "applied-ai-product-engineer",
    });
  });
});
