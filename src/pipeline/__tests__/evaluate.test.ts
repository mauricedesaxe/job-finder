import { describe, expect, test } from "bun:test";
import {
  EVALUATION_FILTERS,
  EVALUATION_PROFILES,
  type EvaluationCriteria,
  type EvaluationFilter,
  type EvaluationProfile,
} from "../../config";
import type { JobListing } from "../../types";
import { evaluateJob, evaluateSingle, type JobEvaluation } from "../evaluate";

const DUMMY_JOB: JobListing = {
  title: "Senior Engineer",
  company: "TestCo",
  url: "https://example.com/job",
  source: "test",
  keywordsMatched: ["engineer"],
  datePosted: null,
  dateScraped: "2026-01-01",
  description: "A test job",
  location: "Remote",
  profile: "",
};

function makeFilter(name: string): EvaluationCriteria {
  return { name, prompt: `filter: ${name}` };
}

function makeProfile(name: string): EvaluationCriteria {
  return { name, prompt: `profile: ${name}` };
}

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

describe("evaluateJob two-phase flow", () => {
  test("filter fails → job rejected, profiles never called", async () => {
    let profileCalled = false;
    const evaluate = async (
      _job: JobListing,
      criteria: EvaluationCriteria,
    ): Promise<JobEvaluation> => {
      if (criteria.prompt.startsWith("profile:")) profileCalled = true;
      if (criteria.prompt.startsWith("filter:"))
        return { pass: false, reason: "Location mismatch" };
      return { pass: true, reason: "ok" };
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [makeFilter("location-gate")],
      profiles: [makeProfile("crypto")],
      evaluate,
    });

    expect(result.pass).toBe(false);
    expect(result.reason).toBe("Location mismatch");
    expect(profileCalled).toBe(false);
  });

  test("filter throws → job rejected (treated as filter failure)", async () => {
    const evaluate = async (): Promise<JobEvaluation> => {
      throw new Error("API timeout");
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [makeFilter("location-gate")],
      profiles: [makeProfile("crypto")],
      evaluate,
    });

    expect(result.pass).toBe(false);
    expect(result.reason).toContain("location-gate");
    expect(result.reason).toContain("failed");
  });

  test("all filters pass → profiles run", async () => {
    const called: string[] = [];
    const evaluate = async (
      _job: JobListing,
      criteria: EvaluationCriteria,
    ): Promise<JobEvaluation> => {
      called.push(criteria.name);
      return { pass: true, reason: "ok" };
    };

    await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [makeFilter("f1")],
      profiles: [makeProfile("p1")],
      evaluate,
    });

    expect(called).toContain("f1");
    expect(called).toContain("p1");
  });

  test("filters pass + profile passes → job passes with profileName", async () => {
    const evaluate = async (
      _job: JobListing,
      _criteria: EvaluationCriteria,
    ): Promise<JobEvaluation> => {
      return { pass: true, reason: "Looks good" };
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [makeFilter("f1")],
      profiles: [makeProfile("crypto")],
      evaluate,
    });

    expect(result.pass).toBe(true);
    expect(result.profileName).toBe("crypto");
  });

  test("filters pass + all profiles fail → job rejected", async () => {
    const evaluate = async (
      _job: JobListing,
      criteria: EvaluationCriteria,
    ): Promise<JobEvaluation> => {
      if (criteria.prompt.startsWith("filter:")) return { pass: true, reason: "ok" };
      return { pass: false, reason: "Not a match" };
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [makeFilter("f1")],
      profiles: [makeProfile("p1"), makeProfile("p2")],
      evaluate,
    });

    expect(result.pass).toBe(false);
    expect(result.reason).toBe("Not a match");
  });

  test("no filters → profiles run directly", async () => {
    const called: string[] = [];
    const evaluate = async (
      _job: JobListing,
      criteria: EvaluationCriteria,
    ): Promise<JobEvaluation> => {
      called.push(criteria.name);
      return { pass: true, reason: "ok" };
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [],
      profiles: [makeProfile("p1")],
      evaluate,
    });

    expect(result.pass).toBe(true);
    expect(called).toEqual(["p1"]);
  });

  test("no profiles + filters pass → job passes", async () => {
    const evaluate = async (): Promise<JobEvaluation> => {
      return { pass: true, reason: "ok" };
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [makeFilter("f1")],
      profiles: [],
      evaluate,
    });

    expect(result.pass).toBe(true);
    expect(result.reason).toBe("Passed all filters");
  });

  test("no filters + no profiles → job fails", async () => {
    const evaluate = async (): Promise<JobEvaluation> => {
      return { pass: true, reason: "ok" };
    };

    const result = await evaluateJob(DUMMY_JOB, "fake-key", {
      filters: [],
      profiles: [],
      evaluate,
    });

    expect(result.pass).toBe(false);
    expect(result.reason).toBe("No profiles configured");
  });
});
