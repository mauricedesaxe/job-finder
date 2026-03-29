import { describe, expect, test } from "bun:test";
import {
  EVALUATION_FILTERS,
  EVALUATION_PROFILES,
  type EvaluationCriteria,
  type EvaluationFilter,
  type EvaluationProfile,
} from "../../profile";
import { evaluateJob, evaluateSingle, type JobEvaluation } from "../evaluate";

describe("evaluate module exports", () => {
  test("exports evaluateJob function", () => {
    expect(typeof evaluateJob).toBe("function");
  });

  test("exports evaluateSingle function", () => {
    expect(typeof evaluateSingle).toBe("function");
  });

  test("JobEvaluation interface shape is correct", () => {
    const evaluation: JobEvaluation = { pass: true, reason: "test", profileName: "crypto-web3" };
    expect(evaluation.pass).toBe(true);
    expect(evaluation.reason).toBe("test");
    expect(evaluation.profileName).toBe("crypto-web3");
  });
});

describe("profile and filter types", () => {
  test("EVALUATION_PROFILES is an array", () => {
    expect(Array.isArray(EVALUATION_PROFILES)).toBe(true);
    expect(EVALUATION_PROFILES.length).toBeGreaterThan(0);
  });

  test("EVALUATION_FILTERS is an array", () => {
    expect(Array.isArray(EVALUATION_FILTERS)).toBe(true);
  });

  test("profiles satisfy EvaluationCriteria", () => {
    for (const profile of EVALUATION_PROFILES) {
      const criteria: EvaluationCriteria = profile;
      expect(typeof criteria.name).toBe("string");
      expect(typeof criteria.prompt).toBe("string");
    }
  });

  test("EvaluationFilter extends EvaluationCriteria", () => {
    const filter: EvaluationFilter = { name: "test-filter", prompt: "test prompt" };
    const criteria: EvaluationCriteria = filter;
    expect(criteria.name).toBe("test-filter");
  });

  test("EvaluationProfile extends EvaluationCriteria", () => {
    const profile: EvaluationProfile = { name: "test-profile", prompt: "test prompt" };
    const criteria: EvaluationCriteria = profile;
    expect(criteria.name).toBe("test-profile");
  });
});
