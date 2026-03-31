import { describe, expect, test } from "bun:test";
import { basename } from "node:path";
import { evaluateJob } from "../evaluate";
import { collectFixtures, loadFixture } from "./helpers";

const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY as string;

const FIXTURES_DIR = `${import.meta.dir}/fixtures/evaluate`;

describe("full evaluation pipeline (integration)", () => {
  for (const file of collectFixtures(`${FIXTURES_DIR}/pass`)) {
    const name = basename(file, ".md");
    test(`${name} → PASS`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/pass/${file}`);
      const result = await evaluateJob(job, ANTHROPIC_API_KEY, { temperature: 0 });
      expect(result.pass, `Expected PASS but got FAIL: ${result.reason}`).toBe(true);
    }, 60_000);
  }

  for (const file of collectFixtures(`${FIXTURES_DIR}/reject`)) {
    const name = basename(file, ".md");
    test(`${name} → FAIL`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/reject/${file}`);
      const result = await evaluateJob(job, ANTHROPIC_API_KEY, { temperature: 0 });
      expect(result.pass, `Expected FAIL but got PASS: ${result.reason}`).toBe(false);
    }, 60_000);
  }
});
