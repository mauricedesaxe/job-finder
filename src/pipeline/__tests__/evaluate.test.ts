import { describe, expect, test } from "bun:test";
import { evaluateJob, type JobEvaluation } from "../evaluate";

describe("evaluate module exports", () => {
  test("exports evaluateJob function", () => {
    expect(typeof evaluateJob).toBe("function");
  });

  test("JobEvaluation interface shape is correct", () => {
    const evaluation: JobEvaluation = { pass: true, reason: "test" };
    expect(evaluation.pass).toBe(true);
    expect(evaluation.reason).toBe("test");
  });
});
