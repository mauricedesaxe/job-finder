import { test, expect, describe } from "bun:test";

const mod = await import("../evaluate");

describe("evaluate module exports", () => {
  test("exports evaluateJob function", () => {
    expect(typeof mod.evaluateJob).toBe("function");
  });

  test("JobEvaluation interface shape is correct", () => {
    const evaluation: mod.JobEvaluation = { pass: true, reason: "test" };
    expect(evaluation.pass).toBe(true);
    expect(evaluation.reason).toBe("test");
  });
});
