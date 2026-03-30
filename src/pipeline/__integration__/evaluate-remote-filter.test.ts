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

describe("remote-europe-eligible filter (integration)", () => {
  for (const file of collectFixtures(`${FIXTURES_DIR}/pass`)) {
    const name = basename(file, ".md");
    test(`${name} → PASS`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/pass/${file}`);
      const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
      expect(result.pass, `Expected PASS but got FAIL: ${result.reason}`).toBe(true);
    }, 30_000);
  }

  for (const file of collectFixtures(`${FIXTURES_DIR}/reject`)) {
    const name = basename(file, ".md");
    test(`${name} → FAIL`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/reject/${file}`);
      const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
      expect(result.pass, `Expected FAIL but got PASS: ${result.reason}`).toBe(false);
    }, 30_000);
  }
});
