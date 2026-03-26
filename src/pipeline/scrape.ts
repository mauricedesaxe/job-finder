import type { JobListing, ScrapioConfig } from "../types";
import { fetchViaJina } from "./search";

export function extractCompanyFromUrl(url: string): string {
  try {
    const { hostname, pathname } = new URL(url);
    const segments = pathname.split("/").filter(Boolean);

    // All three boards use /companyname/... pattern
    if (
      hostname.includes("ashbyhq.com") ||
      hostname.includes("lever.co") ||
      hostname.includes("greenhouse.io")
    ) {
      return segments[0] ?? "Unknown";
    }

    return segments[0] ?? "Unknown";
  } catch {
    return "Unknown";
  }
}

export function detectSource(url: string): string {
  if (url.includes("ashbyhq.com")) return "ashbyhq";
  if (url.includes("lever.co")) return "lever";
  if (url.includes("greenhouse.io")) return "greenhouse";
  return "other";
}

export function extractTitle(markdown: string): string {
  // Look for first # heading
  const headingMatch = markdown.match(/^#\s+(.+)$/m);
  if (headingMatch) return headingMatch[1].trim();

  // Fallback: look for **bold** title near the top
  const boldMatch = markdown.match(/\*\*(.+?)\*\*/);
  if (boldMatch) return boldMatch[1].trim();

  return "Unknown Position";
}

export function extractDatePosted(markdown: string): string | null {
  // Common patterns for date posted across job boards
  const patterns = [
    /(?:posted|published|date)\s*(?:on|:)?\s*(\w+\s+\d{1,2},?\s+\d{4})/i,
    /(?:posted|published|date)\s*(?:on|:)?\s*(\d{4}-\d{2}-\d{2})/i,
    /(?:posted|published|date)\s*(?:on|:)?\s*(\d{1,2}\/\d{1,2}\/\d{4})/i,
  ];

  for (const pattern of patterns) {
    const match = markdown.match(pattern);
    if (match) {
      const date = new Date(match[1]);
      if (!isNaN(date.getTime())) {
        return date.toISOString().split("T")[0];
      }
    }
  }

  return null;
}

export function parseJobDetails(
  markdown: string,
  url: string,
  keyword: string,
): JobListing {
  return {
    title: extractTitle(markdown),
    company: extractCompanyFromUrl(url),
    url,
    source: detectSource(url),
    keywordsMatched: [keyword],
    datePosted: extractDatePosted(markdown),
    dateScraped: new Date().toISOString().split("T")[0],
    description: markdown.slice(0, 8000),
    location: "",
  };
}

export async function scrapeJobPage(
  url: string,
  config: Pick<ScrapioConfig, "jinaBaseUrl" | "jinaApiKey">,
): Promise<string> {
  return fetchViaJina(url, config);
}
