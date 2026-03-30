import { describe, expect, test } from "bun:test";
import { basename } from "node:path";
import { Glob } from "bun";
import { EVALUATION_FILTERS } from "../../config/evaluation";
import type { JobListing } from "../../types";
import { evaluateSingle } from "../evaluate";
import { detectSource } from "../scrape";

const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY as string;
const remoteFilter = EVALUATION_FILTERS.find(
  (f) => f.name === "remote-europe-eligible",
) as (typeof EVALUATION_FILTERS)[number];

const FIXTURES_DIR = `${import.meta.dir}/fixtures/remote`;

async function loadFixture(path: string): Promise<JobListing> {
  const content = await Bun.file(path).text();
  const title = content.match(/^Title:\s*(.+)$/m)?.[1] ?? basename(path, ".md");
  const url =
    content.match(/^URL Source:\s*(.+)$/m)?.[1] ?? `https://example.com/${basename(path, ".md")}`;

  return {
    title,
    company: basename(path, ".md"),
    url,
    source: detectSource(url),
    keywordsMatched: ["test"],
    datePosted: null,
    dateScraped: "2026-03-30",
    description: content,
    location: "",
    profile: "",
  };
}

function collectFixtures(dir: string): string[] {
  const glob = new Glob("*.md");
  return Array.from(glob.scanSync(dir)).sort();
}

describe("remote-europe-eligible filter (integration)", () => {
  for (const file of collectFixtures(`${FIXTURES_DIR}/pass`)) {
    const name = basename(file, ".md");
    test(`${name} → PASS`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/pass/${file}`);
      const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
      expect(result.pass).toBe(true);
    }, 30_000);
  }

  for (const file of collectFixtures(`${FIXTURES_DIR}/reject`)) {
    const name = basename(file, ".md");
    test(`${name} → FAIL`, async () => {
      const job = await loadFixture(`${FIXTURES_DIR}/reject/${file}`);
      const result = await evaluateSingle(job, remoteFilter, ANTHROPIC_API_KEY);
      expect(result.pass).toBe(false);
    }, 30_000);
  }
});
