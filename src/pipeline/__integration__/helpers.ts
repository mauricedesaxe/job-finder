import { existsSync } from "node:fs";
import { basename } from "node:path";
import { Glob } from "bun";
import type { JobListing } from "../../types";
import { detectSource, extractCompanyFromUrl } from "../scrape";

export async function loadFixture(path: string): Promise<JobListing> {
  const content = await Bun.file(path).text();
  const title = content.match(/^Title:\s*(.+)$/m)?.[1] ?? basename(path, ".md");
  const url =
    content.match(/^URL Source:\s*(.+)$/m)?.[1] ?? `https://example.com/${basename(path, ".md")}`;

  return {
    title,
    company: extractCompanyFromUrl(url),
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

export function collectFixtures(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const glob = new Glob("*.md");
  return Array.from(glob.scanSync(dir)).sort();
}
