import { describe, expect, test } from "bun:test";
import { basename } from "node:path";
import type { JobListing } from "../../types";
import { evaluateJob, type JobEvaluation } from "../evaluate";
import { structuralFilter } from "../structuralFilter";
import { collectFixtures, loadFixture } from "./helpers";

async function evaluateFullPipeline(
  job: JobListing,
  apiKey: string,
  options: { temperature?: number; model?: string },
): Promise<JobEvaluation> {
  const structural = structuralFilter(job);
  if (!structural.pass) return { pass: false, reason: structural.reason };
  return evaluateJob(job, apiKey, options);
}

const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY as string;
const LLM_MODEL = process.env.LLM_MODEL ?? "google/gemini-2.5-flash";

const FIXTURES_DIR = `${import.meta.dir}/fixtures/evaluate`;

// Alex prefers fewer false positives over fewer false negatives — too many
// jobs already flood the To-Review pile. FP threshold is therefore stricter.
// Both numbers should ratchet down as we close eval gaps.
const FP_RATE_MAX = 0.3; // % of reject fixtures the eval wrongly passes
const FN_RATE_MAX = 0.3; // % of pass fixtures the eval wrongly fails

type Result = { name: string; expected: boolean; actual: boolean; reason: string };

const results: Result[] = [];

describe("full evaluation pipeline (integration)", () => {
  for (const file of collectFixtures(`${FIXTURES_DIR}/pass`)) {
    const name = basename(file, ".md");
    test(`${name} → PASS`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/pass/${file}`);
      const result = await evaluateFullPipeline(job, OPENROUTER_API_KEY, {
        temperature: 0,
        model: LLM_MODEL,
      });
      results.push({ name, expected: true, actual: result.pass, reason: result.reason });
    }, 60_000);
  }

  for (const file of collectFixtures(`${FIXTURES_DIR}/reject`)) {
    const name = basename(file, ".md");
    test(`${name} → FAIL`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/reject/${file}`);
      const result = await evaluateFullPipeline(job, OPENROUTER_API_KEY, {
        temperature: 0,
        model: LLM_MODEL,
      });
      results.push({ name, expected: false, actual: result.pass, reason: result.reason });
    }, 60_000);
  }

  test("FP and FN rates meet thresholds", () => {
    const total = results.length;
    const expectedPass = results.filter((r) => r.expected === true);
    const expectedFail = results.filter((r) => r.expected === false);
    const fp = expectedFail.filter((r) => r.actual === true); // wrongly passed
    const fn = expectedPass.filter((r) => r.actual === false); // wrongly failed
    const fpRate = expectedFail.length > 0 ? fp.length / expectedFail.length : 0;
    const fnRate = expectedPass.length > 0 ? fn.length / expectedPass.length : 0;
    const correct = total - fp.length - fn.length;

    for (const m of [...fp, ...fn]) {
      const direction = m.expected ? "PASS→FAIL (FN)" : "FAIL→PASS (FP)";
      console.log(`  MISCLASSIFIED [${direction}]: ${m.name}: ${m.reason}`);
    }
    console.log(`  Overall: ${correct}/${total} (${((correct / total) * 100).toFixed(1)}%)`);
    console.log(
      `  FP rate: ${fp.length}/${expectedFail.length} (${(fpRate * 100).toFixed(1)}%) — must be ≤ ${(FP_RATE_MAX * 100).toFixed(0)}%`,
    );
    console.log(
      `  FN rate: ${fn.length}/${expectedPass.length} (${(fnRate * 100).toFixed(1)}%) — must be ≤ ${(FN_RATE_MAX * 100).toFixed(0)}%`,
    );

    expect(total).toBeGreaterThan(0);
    expect(fpRate).toBeLessThanOrEqual(FP_RATE_MAX);
    expect(fnRate).toBeLessThanOrEqual(FN_RATE_MAX);
  }, 5_000);
});
