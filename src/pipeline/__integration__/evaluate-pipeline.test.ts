import { afterAll, beforeAll, describe, expect, setDefaultTimeout, test } from "bun:test";
import { basename } from "node:path";
import { z } from "zod/v4";
import { Semaphore } from "../../concurrency";
import { initLangSmith, shutdownLangSmith } from "../../services/langsmith";
import type { JobListing } from "../../types";
import { evaluateJob, type JobEvaluation } from "../evaluate";
import { structuralFilter } from "../structuralFilter";
import { collectFixtures, loadFixture } from "./helpers";

async function evaluateFullPipeline(job: JobListing): Promise<JobEvaluation> {
  const structural = structuralFilter(job);
  if (!structural.pass) return { pass: false, reason: structural.reason };
  return evaluateJob(job);
}

const IntegrationConfigSchema = z.object({
  langsmithApiKey: z.string().min(1, "LANGSMITH_API_KEY is required"),
  langsmithEndpoint: z.string().url().default("https://eu.api.smith.langchain.com"),
  langsmithProject: z.string().default("job-finder-integration-tests"),
  openrouterApiKey: z.string().min(1, "OPENROUTER_API_KEY is required"),
});
const integrationConfig = IntegrationConfigSchema.parse({
  langsmithApiKey: process.env.LANGSMITH_API_KEY,
  langsmithEndpoint: process.env.LANGSMITH_ENDPOINT,
  langsmithProject: process.env.LANGSMITH_PROJECT,
  openrouterApiKey: process.env.OPENROUTER_API_KEY,
});

const FIXTURES_DIR = `${import.meta.dir}/fixtures/evaluate`;

// Alex prefers fewer false positives over fewer false negatives — too many
// jobs already flood the To-Review pile. FP threshold is therefore stricter
// in spirit, even though it's the looser of the two right now.
//
// First real measurement after the Bun 1.2.23 parser fix (which had been
// silently skipping this assertion in CI for months): FP ≈ 25%, FN ≈ 2.4%
// against the 36-pass / 40-reject fixture set.
//
// FN is locked in at 0.10 to hold the 2.4% achieved rate — any drift
// upward must be a deliberate change, not noise.
//
// FP was tightened from 0.30 → 0.20 → 0.15 across this session. First
// pass closed prompt gaps from ROADMAP §5.1 (DevOps/K8s primary, now
// rule-quality §8 framed as responsibilities-not-skills) and §5.4
// (L1/consensus depth, now rule-quality §9 framed as "build the chain
// itself" not "uses chain adjacent skills"); structural filter now
// rejects careers-index URLs and "general application" framing
// prefixed by company name. Second pass added the
// `cheap-shop-placement` filter (4th LLM filter in
// getEvaluationFilters) that fails when ≥2 cheap-shop signals stack —
// recruiter-placement framing, junior-bar-as-senior, comp-obscured,
// LATAM-only talent pool, low-code-required, contractor + foreign
// client hours. That closed pavago, huzzle, via-hatchit, south-geeks
// without breaking mlabs or infinity-constellation.
// Achieved measurement after both passes: FP ≈ 7.5-10%, FN ≈ 5%.
// 0.15 has 2-fixture headroom for LLM noise and tool_call flakes.
// Remaining FPs (abacus borderline MLE, wayflyer Python/Django CRUD,
// tem Senior Staff agent platform) are tracked in ROADMAP §5.8.
const FP_RATE_MAX = 0.15; // % of reject fixtures the eval wrongly passes
const FN_RATE_MAX = 0.1; // % of pass fixtures the eval wrongly fails

// Run fixtures in parallel through the LLM. evaluateJob already fans out the
// 4 filters + 2 profiles in parallel, so each fixture is ~6 LLM calls. With
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
    await initLangSmith({
      apiKey: integrationConfig.langsmithApiKey,
      endpoint: integrationConfig.langsmithEndpoint,
      project: integrationConfig.langsmithProject,
      openrouterApiKey: integrationConfig.openrouterApiKey,
    });

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
            const result = await evaluateFullPipeline(job);
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

  afterAll(shutdownLangSmith);

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
