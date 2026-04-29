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
const ACCURACY_THRESHOLD = 0.75;

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

  test("accuracy meets threshold", () => {
    const total = results.length;
    const correct = results.filter((r) => r.expected === r.actual).length;
    const accuracy = correct / total;

    const misclassified = results.filter((r) => r.expected !== r.actual);
    for (const m of misclassified) {
      console.log(
        `  MISCLASSIFIED: ${m.name} (expected ${m.expected ? "PASS" : "FAIL"}, got ${m.actual ? "PASS" : "FAIL"}): ${m.reason}`,
      );
    }
    console.log(`  Accuracy: ${correct}/${total} (${(accuracy * 100).toFixed(1)}%)`);

    expect(total).toBeGreaterThan(0);
    expect(accuracy).toBeGreaterThanOrEqual(ACCURACY_THRESHOLD);
  }, 5_000);
});
