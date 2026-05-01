import { beforeAll, describe, expect, setDefaultTimeout, test } from "bun:test";
import { basename } from "node:path";
import { Semaphore } from "../../concurrency";
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
//
// Current achieved rates with the role-quality filter and the softened
// ai-engineering profile: FP ≈ 17%, FN ≈ 8%. Thresholds set with a small
// buffer above achieved rates to absorb LLM non-determinism (temperature 0
// is not deterministic on these models).
const FP_RATE_MAX = 0.22; // % of reject fixtures the eval wrongly passes
const FN_RATE_MAX = 0.15; // % of pass fixtures the eval wrongly fails

// Run fixtures in parallel through the LLM. evaluateJob already fans out the
// 3 filters + 4 profiles in parallel, so each fixture is ~7 LLM calls. With
// 70+ fixtures that's 500+ in-flight calls if we go fully unbounded —
// OpenRouter would throttle. Cap at 12 concurrent fixtures.
const FIXTURE_CONCURRENCY = 12;
const PARALLEL_RUN_TIMEOUT_MS = 600_000;

// Bun 1.2.23 rejects the (fn, timeoutMs) shape of `beforeAll` at runtime even
// though bun-types accepts it. Use the module-scoped default instead — the
// per-test 5_000ms timeout below still wins because per-test takes precedence.
setDefaultTimeout(PARALLEL_RUN_TIMEOUT_MS);

type Result = { name: string; expected: boolean; actual: boolean; reason: string };

const results: Result[] = [];

describe("full evaluation pipeline (integration)", () => {
  beforeAll(async () => {
    const passFiles = collectFixtures(`${FIXTURES_DIR}/pass`).map((file) => ({
      file,
      dir: "pass" as const,
      expected: true,
    }));
    const rejectFiles = collectFixtures(`${FIXTURES_DIR}/reject`).map((file) => ({
      file,
      dir: "reject" as const,
      expected: false,
    }));
    const all = [...passFiles, ...rejectFiles];

    const sem = new Semaphore(FIXTURE_CONCURRENCY);
    const evaluations = await Promise.all(
      all.map(({ file, dir, expected }) =>
        sem.run(async (): Promise<Result> => {
          const name = basename(file, ".md");
          try {
            const job = await loadFixture(`${FIXTURES_DIR}/${dir}/${file}`);
            const result = await evaluateFullPipeline(job, OPENROUTER_API_KEY, {
              temperature: 0,
              model: LLM_MODEL,
            });
            return { name, expected, actual: result.pass, reason: result.reason };
          } catch (err) {
            // A single bad LLM response or transient API error must not kill
            // the whole batch. Record the fixture as unknown so it shows up
            // as a misclassification on whichever side is wrong.
            const message = err instanceof Error ? err.message : String(err);
            return { name, expected, actual: !expected, reason: `errored: ${message}` };
          }
        }),
      ),
    );
    for (const e of evaluations) results.push(e);
  });

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
