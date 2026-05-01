import { beforeAll, describe, expect, test } from "bun:test";
import { basename } from "node:path";
import { Semaphore } from "../../concurrency";
import { getEvaluationFilters } from "../../config/evaluation";
import { evaluateSingle } from "../evaluate";
import { collectFixtures, loadFixture } from "./helpers";

// This suite tests the remote-europe-eligible filter against fixtures whose
// reject/pass signal lives in the `## ATS Structured Data` block prepended by
// `formatAtsBlock` (see services/ats/index.ts). The block carries
// employer-set workplaceType + country + locations metadata, which is more
// authoritative than Jina-scraped page headers. The filter must:
//
//   - treat workplaceType=OnSite as a hard reject regardless of body
//   - treat workplaceType=Hybrid as reject UNLESS body explicitly contradicts
//     ("100% remote with optional offices"-style language)
//   - treat workplaceType=Remote + single non-EU country as reject when the
//     body is silent about geo eligibility
//   - PASS workplaceType=Remote when the locations include any EU country
//   - PASS workplaceType=Remote + single non-EU country when the body
//     explicitly says worldwide/global hiring
//   - reject locations dominated by cheap-labor countries with minimal EU
//     presence (probable budget signal)
//
// Run only the location filter (not the full pipeline) so verdicts are
// attributable to ATS-aware location handling, not coincidental rejects from
// role-quality or compensation filters that share the fixture body.

const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY as string;
const LLM_MODEL = process.env.LLM_MODEL ?? "google/gemini-2.5-flash";
const remoteFilter = getEvaluationFilters().find(
  (f) => f.name === "remote-europe-eligible",
) as ReturnType<typeof getEvaluationFilters>[number];

const FIXTURES_DIR = `${import.meta.dir}/fixtures/evaluate`;

// FP costs more than FN (false positives flood To-Review). The suite is small
// (6 reject + 3 pass) so granularity is coarse — adjust as fixtures are added.
const FP_RATE_MAX = 0.17; // ≤ 1 of 6 reject fixtures may leak through
const FN_RATE_MAX = 0.34; // ≤ 1 of 3 pass fixtures may wrongly fail

// Each fixture is one filter call. Cap concurrency so we don't trip
// OpenRouter rate limits when this suite grows.
const FIXTURE_CONCURRENCY = 8;
const PARALLEL_RUN_TIMEOUT_MS = 300_000;

type Result = { name: string; expected: boolean; actual: boolean; reason: string };

const results: Result[] = [];

describe("ATS-aware remote-europe-eligible filter (integration)", () => {
  beforeAll(async () => {
    const passFiles = collectFixtures(`${FIXTURES_DIR}/pass/ats`).map((file) => ({
      file,
      dir: "pass/ats" as const,
      expected: true,
    }));
    const rejectFiles = collectFixtures(`${FIXTURES_DIR}/reject/ats`).map((file) => ({
      file,
      dir: "reject/ats" as const,
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
            const result = await evaluateSingle(job, remoteFilter, OPENROUTER_API_KEY, undefined, {
              temperature: 0,
              model: LLM_MODEL,
            });
            return { name, expected, actual: result.pass, reason: result.reason };
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            return { name, expected, actual: !expected, reason: `errored: ${message}` };
          }
        }),
      ),
    );
    for (const e of evaluations) results.push(e);
  }, PARALLEL_RUN_TIMEOUT_MS);

  test("FP and FN rates meet thresholds", () => {
    const total = results.length;
    const expectedPass = results.filter((r) => r.expected === true);
    const expectedFail = results.filter((r) => r.expected === false);
    const fp = expectedFail.filter((r) => r.actual === true);
    const fn = expectedPass.filter((r) => r.actual === false);
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
