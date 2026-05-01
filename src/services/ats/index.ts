import { logger } from "../../logger";
import { clearAshbyCache, fetchAshbyJob } from "./ashby";
import { fetchGreenhouseJob } from "./greenhouse";
import { fetchLeverJob } from "./lever";
import type { AtsJobData, AtsSource, Fetcher } from "./types";

export type { AtsJobData, AtsSource } from "./types";
export { clearAshbyCache };

const log = logger.child({ component: "ats" });

export function detectAtsSource(url: string): AtsSource | null {
  if (url.includes("lever.co")) return "lever";
  if (url.includes("ashbyhq.com")) return "ashby";
  if (url.includes("greenhouse.io")) return "greenhouse";
  return null;
}

export async function fetchAtsData(
  url: string,
  fetcher: Fetcher = fetch,
): Promise<AtsJobData | null> {
  const source = detectAtsSource(url);
  if (!source) return null;

  try {
    switch (source) {
      case "lever":
        return await fetchLeverJob(url, fetcher);
      case "ashby":
        return await fetchAshbyJob(url, fetcher);
      case "greenhouse":
        return await fetchGreenhouseJob(url, fetcher);
    }
  } catch (err) {
    log.warn({ err, url }, "ATS dispatcher unexpected error");
    return null;
  }
}

/**
 * Deterministic reject based on ATS metadata. workplaceType=OnSite is
 * essentially never a false reject in practice — employers who set OnSite in
 * their ATS mean it, and the body cannot override that signal. Hybrid is
 * deliberately NOT a hard reject: the MONEI case showed that some Hybrid
 * listings are actually 100% remote in the body, and that judgement is left
 * to the location-eligibility filter.
 *
 * Returns { pass: true } when there's nothing to reject (including null data,
 * meaning ATS enrichment was unavailable).
 */
export function atsStructuralFilter(data: AtsJobData | null): { pass: boolean; reason: string } {
  if (!data) return { pass: true, reason: "" };
  if (data.workplaceType === "OnSite") {
    const where = data.location || data.country || "unspecified";
    return { pass: false, reason: `ATS workplaceType=OnSite (${where})` };
  }
  return { pass: true, reason: "" };
}

export function formatAtsBlock(data: AtsJobData): string {
  const lines = [`## ATS Structured Data (from ${data.source} API)`];
  if (data.location) lines.push(`- Primary location: ${data.location}`);
  if (data.locations.length > 0) lines.push(`- All listed locations: ${data.locations.join(", ")}`);
  lines.push(`- Workplace type: ${data.workplaceType ?? "unspecified"}`);
  if (data.country) lines.push(`- Country (HQ): ${data.country}`);
  lines.push("---");
  return lines.join("\n");
}
