import { describe, expect, test } from "bun:test";
import { basename } from "node:path";
import { EVALUATION_FILTERS } from "../../config/evaluation";
import { evaluateSingle } from "../evaluate";
import { collectFixtures, loadFixture } from "./helpers";

const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY as string;
const remoteFilter = EVALUATION_FILTERS.find(
  (f) => f.name === "remote-europe-eligible",
) as (typeof EVALUATION_FILTERS)[number];

const FIXTURES_DIR = `${import.meta.dir}/fixtures/remote`;
const ACCURACY_THRESHOLD = 0.75;

type Result = { name: string; expected: boolean; actual: boolean; reason: string };

const results: Result[] = [];

describe("remote-europe-eligible filter (integration)", () => {
  for (const file of collectFixtures(`${FIXTURES_DIR}/pass`)) {
    const name = basename(file, ".md");
    test(`${name} → PASS`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/pass/${file}`);
      const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY, undefined, {
        temperature: 0,
      });
      results.push({ name, expected: true, actual: result.pass, reason: result.reason });
    }, 30_000);
  }

  for (const file of collectFixtures(`${FIXTURES_DIR}/reject`)) {
    const name = basename(file, ".md");
    test(`${name} → FAIL`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/reject/${file}`);
      const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY, undefined, {
        temperature: 0,
      });
      results.push({ name, expected: false, actual: result.pass, reason: result.reason });
    }, 30_000);
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
