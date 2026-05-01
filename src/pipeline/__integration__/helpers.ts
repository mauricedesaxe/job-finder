import { existsSync } from "node:fs";
import { basename } from "node:path";
import { Glob } from "bun";
import type { AtsJobData, AtsSource, WorkplaceType } from "../../services/ats/types";
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

/**
 * Parse the `## ATS Structured Data` block out of a fixture's markdown so the
 * integration test can exercise the same deterministic filter the production
 * pipeline runs. Returns null if the fixture has no ATS block (regular
 * body-only fixtures). The parser is forgiving: missing fields become null /
 * empty per the AtsJobData contract.
 */
export function parseAtsBlockFromFixture(markdown: string): AtsJobData | null {
  const sourceMatch = markdown.match(/## ATS Structured Data \(from (\w+) API\)/);
  const sourceRaw = sourceMatch?.[1];
  if (!sourceRaw) return null;
  const source = sourceRaw as AtsSource;

  const primaryRaw = markdown.match(/^- Primary location:\s*(.+)$/m)?.[1];
  const allRaw = markdown.match(/^- All listed locations:\s*(.+)$/m)?.[1];
  const workplaceRaw = markdown.match(/^- Workplace type:\s*(.+)$/m)?.[1]?.trim();
  const countryRaw = markdown.match(/^- Country \(HQ\):\s*(.+)$/m)?.[1];

  const workplaceType: WorkplaceType | null =
    workplaceRaw === "Remote" || workplaceRaw === "Hybrid" || workplaceRaw === "OnSite"
      ? workplaceRaw
      : null;

  return {
    source,
    location: primaryRaw?.trim() ?? "",
    locations: allRaw
      ? allRaw
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      : [],
    workplaceType,
    country: countryRaw?.trim() ?? null,
  };
}
